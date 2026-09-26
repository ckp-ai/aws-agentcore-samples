import json
import uuid
import boto3

client = boto3.client(
    "bedrock-agentcore",
    region_name="us-east-1",
)

payload = {
    "prompt": (
        "Estimate the recurring monthly cost of this CDK deployment. "
        "Clearly state all assumptions and excluded costs."
    ),
    "region": "us-east-1",
    "cdk_project_s3_uri": (
        "s3://ckp-agentcore-cost-input-927087719421-us-east-1/"
        "deployments/sample-cdk-deployment.zip"
    ),
}

response = client.invoke_agent_runtime(
    agentRuntimeArn=(
        "arn:aws:bedrock-agentcore:us-east-1:927087719421:runtime/"
        "CostEstimatorProject_CostEstimatorAgent-msjEpEG4p6"
    ),
    runtimeSessionId=str(uuid.uuid4()),
    qualifier="DEFAULT",
    payload=json.dumps(payload).encode("utf-8"),
)

print(response["response"].read().decode("utf-8"))
