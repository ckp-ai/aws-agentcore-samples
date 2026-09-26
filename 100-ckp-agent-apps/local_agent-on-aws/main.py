"""AgentCore agent using AWS's cost-estimator Agent Skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from bedrock_agentcore.runtime import (
    BedrockAgentCoreApp,
    BedrockAgentCoreContext,
)
from strands import Agent, tool
from strands.models.bedrock import BedrockModel


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_DIRECTORY = Path(__file__).resolve().parent
SKILL_DIRECTORY = APP_DIRECTORY / "skills" / "cost-estimator"
SKILL_FILE = SKILL_DIRECTORY / "SKILL.md"
SKILL_SCRIPT = SKILL_DIRECTORY / "scripts" / "fetch-aws-pricing.py"

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "global.amazon.nova-2-lite-v1:0")

MAX_PROJECT_BYTES = 25 * 1024 * 1024
MAX_SOURCE_CHARACTERS = 120_000

app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Agent Skill loader
# ---------------------------------------------------------------------------

def read_required_file(path: Path) -> str:
    """Read a required skill file."""

    if not path.exists():
        raise RuntimeError(f"Required skill file is missing: {path}")

    return path.read_text(encoding="utf-8")


def load_cost_estimator_skill() -> str:
    """Load the skill and only the references needed for this agent."""

    skill = read_required_file(SKILL_FILE)
    cdk_analysis = read_required_file(
        SKILL_DIRECTORY / "references" / "cdk-analysis.md"
    )
    pricing_api = read_required_file(
        SKILL_DIRECTORY / "references" / "pricing-api.md"
    )
    report_generation = read_required_file(
        SKILL_DIRECTORY / "references" / "report-generation.md"
    )

    return f"""
You have access to the following AWS Agent Skill.

<agent-skill>
{skill}
</agent-skill>

<cdk-analysis-reference>
{cdk_analysis}
</cdk-analysis-reference>

<pricing-api-reference>
{pricing_api}
</pricing-api-reference>

<report-generation-reference>
{report_generation}
</report-generation-reference>

Additional runtime rules:

1. Treat the estimate as an approximation, not an AWS bill or quotation.
2. State all usage assumptions.
3. Never invent an AWS price.
4. Use query_aws_pricing for unit prices.
5. If pricing cannot be retrieved, mark that item as unresolved.
6. Never claim that an estimate includes data transfer unless it was explicitly
   calculated.
7. Present recurring monthly cost separately from one-time cost.
8. Do not deploy, modify or delete AWS resources.
9. A public NAT Gateway requires at least one Elastic IP address. Treat each
   Elastic IP as a chargeable public IPv4 address.
10. Never state that a public NAT Gateway has no Elastic IP address.
11. Include public IPv4 addresses as separate material cost items. Retrieve
    their current price when possible; if pricing cannot be retrieved, mark
    the cost as unresolved instead of omitting it.
12. Show NAT Gateway data-processing cost as a variable per-GB charge, even
    when assumed monthly traffic is zero.
    13. Every identified fixed recurring charge must appear as a line item in the
    monthly cost table and must be included in the grand total.
14. Never place the Elastic IP or public IPv4 address required by a public NAT
    Gateway under excluded costs.
15. For standard public NAT Gateways, assume one chargeable public IPv4 address
    per NAT Gateway unless the infrastructure specifies a different count.
16. Retrieve public IPv4 pricing by calling query_aws_pricing with offer code
    AmazonEC2 and a usagetype filter containing PublicIPv4:InUseAddress.
17. Before returning the report, reconcile the grand total against the sum of
    every fixed recurring line item. If the values do not match, correct the
    grand total.
"""


try:
    SKILL_INSTRUCTIONS = load_cost_estimator_skill()
    SKILL_LOAD_ERROR: str | None = None
except RuntimeError as _skill_error:
    SKILL_INSTRUCTIONS = None
    SKILL_LOAD_ERROR = str(_skill_error)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    """Split an S3 URI into bucket and object key."""

    match = re.fullmatch(r"s3://([^/]+)/(.+)", s3_uri)

    if not match:
        raise ValueError(
            "Expected an S3 URI such as "
            "'s3://my-bucket/deployments/application.zip'."
        )

    return match.group(1), match.group(2)


def safe_extract(zip_file: zipfile.ZipFile, destination: Path) -> None:
    """Extract a ZIP archive while preventing path traversal."""

    resolved_destination = destination.resolve()

    for member in zip_file.infolist():
        member_path = (destination / member.filename).resolve()

        if (
            member_path != resolved_destination
            and resolved_destination not in member_path.parents
        ):
            raise ValueError(
                f"Unsafe ZIP member path: {member.filename}"
            )

    zip_file.extractall(destination)


def project_workspace(s3_uri: str) -> Path:
    """Return a repeatable temporary workspace for an S3 project."""

    digest = hashlib.sha256(s3_uri.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "cost-estimator" / digest


# ---------------------------------------------------------------------------
# Tools exposed to Nova
# ---------------------------------------------------------------------------

@tool
def inspect_cdk_project(s3_uri: str) -> str:
    """Download and inspect a zipped AWS CDK project stored in Amazon S3.

    Args:
        s3_uri: S3 URI of a ZIP file containing the CDK project.

    Returns:
        Relevant CDK source files and architecture documentation. The result
        is used to identify resources that require cost estimation.

    This tool only reads the S3 object. It does not run CDK or deploy anything.
    """

    try:
        bucket, key = parse_s3_uri(s3_uri)
        workspace = project_workspace(s3_uri)
        source_directory = workspace / "source"
        archive_path = workspace / "project.zip"

        workspace.mkdir(parents=True, exist_ok=True)

        s3 = boto3.client("s3", region_name=AWS_REGION)
        metadata = s3.head_object(Bucket=bucket, Key=key)

        content_length = metadata.get("ContentLength", 0)

        if content_length > MAX_PROJECT_BYTES:
            return json.dumps(
                {
                    "status": "error",
                    "message": (
                        f"Project archive is {content_length} bytes; "
                        f"maximum allowed size is {MAX_PROJECT_BYTES}."
                    ),
                }
            )

        s3.download_file(bucket, key, str(archive_path))

        source_directory.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive_path) as archive:
            safe_extract(archive, source_directory)

        supported_names = {
            "cdk.json",
            "README.md",
            "ARCHITECTURE.md",
            "package.json",
        }
        supported_suffixes = {
            ".ts",
            ".tsx",
            ".py",
            ".json",
            ".yaml",
            ".yml",
            ".md",
        }
        ignored_directories = {
            "node_modules",
            ".git",
            "cdk.out",
            ".venv",
            "dist",
            "build",
        }

        discovered_files: list[dict[str, str]] = []
        consumed_characters = 0

        for path in sorted(source_directory.rglob("*")):
            if not path.is_file():
                continue

            relative_path = path.relative_to(source_directory)

            if any(part in ignored_directories for part in relative_path.parts):
                continue

            if (
                path.name not in supported_names
                and path.suffix.lower() not in supported_suffixes
            ):
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            remaining = MAX_SOURCE_CHARACTERS - consumed_characters

            if remaining <= 0:
                break

            content = content[:remaining]
            consumed_characters += len(content)

            discovered_files.append(
                {
                    "path": str(relative_path),
                    "content": content,
                }
            )

        return json.dumps(
            {
                "status": "success",
                "project_source": s3_uri,
                "files_returned": len(discovered_files),
                "truncated": consumed_characters >= MAX_SOURCE_CHARACTERS,
                "files": discovered_files,
            }
        )

    except (ValueError, zipfile.BadZipFile) as error:
        return json.dumps(
            {
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error),
            }
        )

    except (ClientError, BotoCoreError) as error:
        return json.dumps(
            {
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error),
            }
        )


@tool
def query_aws_pricing(
    region: str,
    offer_code: str,
    filters: dict[str, str],
) -> str:
    """Retrieve current public AWS unit pricing using the skill's script.

    Args:
        region: Deployment Region, for example us-east-1.
        offer_code: AWS Price List offer code, such as AmazonEC2,
            AmazonECS or AmazonRDS.
        filters: Product attributes to match, for example
            {"instanceType": "db.t4g.medium",
             "databaseEngine": "PostgreSQL"}.

    Returns:
        Matching AWS Price List products and their on-demand unit prices.
    """

    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d+", region):
        return json.dumps(
            {
                "status": "error",
                "message": f"Invalid AWS Region: {region}",
            }
        )

    if not re.fullmatch(r"[A-Za-z0-9]+", offer_code):
        return json.dumps(
            {
                "status": "error",
                "message": f"Invalid offer code: {offer_code}",
            }
        )

    command = [
        "python3",
        str(SKILL_SCRIPT),
        region,
        "--offer-code",
        offer_code,
    ]

    for key, value in filters.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Invalid pricing filter key: {key}",
                }
            )

        command.extend(["--filter", f"{key}={value}"])

    cache_directory = (
        Path(tempfile.gettempdir())
        / "cost-estimator"
        / "pricing-cache"
    )
    command.extend(["--cache-dir", str(cache_directory)])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            shell=False,
        )

        if completed.returncode != 0:
            return json.dumps(
                {
                    "status": "error",
                    "exit_code": completed.returncode,
                    "message": completed.stderr[-2000:],
                }
            )

        return completed.stdout

    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "status": "error",
                "message": "AWS pricing query timed out.",
            }
        )


@tool
def calculate_monthly_cost(
    unit_price: float,
    monthly_quantity: float,
) -> str:
    """Calculate an estimated monthly cost from a retrieved unit price.

    Args:
        unit_price: Price for one billing unit.
        monthly_quantity: Estimated number of billing units per month.

    Returns:
        The estimated monthly cost rounded to four decimal places.
    """

    if unit_price < 0 or monthly_quantity < 0:
        return json.dumps(
            {
                "status": "error",
                "message": "Pricing values cannot be negative.",
            }
        )

    return json.dumps(
        {
            "unit_price": unit_price,
            "monthly_quantity": monthly_quantity,
            "estimated_monthly_cost": round(
                unit_price * monthly_quantity,
                4,
            ),
        }
    )


# ---------------------------------------------------------------------------
# Nova and Strands agent
# ---------------------------------------------------------------------------

model = BedrockModel(
    model_id=MODEL_ID,
    region_name=AWS_REGION,
)

agent = (
    Agent(
        model=model,
        system_prompt=SKILL_INSTRUCTIONS,
        tools=[
            inspect_cdk_project,
            query_aws_pricing,
            calculate_monthly_cost,
        ],
    )
    if SKILL_LOAD_ERROR is None
    else None
)


# ---------------------------------------------------------------------------
# AgentCore entry point
# ---------------------------------------------------------------------------

@app.entrypoint
def invoke(
    payload: dict[str, Any],
    context: BedrockAgentCoreContext,
) -> dict[str, Any]:
    """Invoke the cost-estimation agent."""

    if SKILL_LOAD_ERROR is not None:
        return {
            "status": "error",
            "message": f"Agent skill failed to load: {SKILL_LOAD_ERROR}",
        }

    prompt = payload.get("prompt")
    cdk_project_s3_uri = payload.get("cdk_project_s3_uri")
    region = payload.get("region", AWS_REGION)

    if not isinstance(prompt, str) or not prompt.strip():
        return {
            "status": "error",
            "message": "A non-empty 'prompt' is required.",
        }

    enriched_prompt = f"""
User request:
{prompt}

Deployment region:
{region}

CDK project:
{cdk_project_s3_uri or "No CDK project was supplied."}

Follow the cost-estimator skill workflow.

If a CDK project was supplied:

1. Call inspect_cdk_project.
2. Identify the AWS resources and relevant configurations.
3. State missing usage assumptions.
4. Use reasonable, clearly labelled assumptions only when necessary.
5. Call query_aws_pricing for every material cost item.
6. Use calculate_monthly_cost for calculations.
7. Return a Markdown cost report.

Do not deploy the project.
"""

    result = agent(enriched_prompt)

    return {
        "status": "success",
        "response": result.message["content"][0]["text"],
        "session_id": getattr(context, "session_id", None),
    }


if __name__ == "__main__":
    app.run()