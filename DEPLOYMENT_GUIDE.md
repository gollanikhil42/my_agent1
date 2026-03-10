# Deployment Guide (Amplify + API + AgentCore)

## 1. Target Architecture
- Frontend: React app hosted in AWS Amplify Hosting.
- Auth: Cognito User Pool (multiple users), app client WITHOUT secret for SPA.
- API: API Gateway HTTP API + Lambda (`backend/lambda/chat_handler.py`).
- Runtime: Existing Bedrock AgentCore runtime (`my_agent1`).

Request path (production):
- Browser (Amplify React) -> API Gateway `/chat` -> Lambda -> AgentCore runtime endpoint -> response -> browser

## 2. Cognito Setup for Multiple Users
- Use your new pool: `us-east-1_l6dPnsMxY`.
- Use your SPA app client: `4hb4q7nat1kqbm6ehsgtq3e2op`.
- App client should have no secret.
- Add users in the same pool (you already did this).
- Ensure custom attributes (department, role) exist if needed.

Callback/sign-out URLs:
- If using Amplify `Authenticator` with username/password only (current app): callback URL is not mandatory.
- If enabling Cognito Hosted UI/social IdP later, configure callback and sign-out URLs:
  - Local: `http://localhost:5173/`
  - Amplify: `https://<your-amplify-domain>/`

## 3. Install Tooling (Windows)
Install SAM CLI (since `sam` is not recognized):

```powershell
winget install Amazon.SAMCLI
```

If `winget` cannot find the package, install manually using AWS SAM CLI Windows installer (MSI), then reopen terminal.

Alternative:

```powershell
choco install aws-sam-cli -y
```

Then restart terminal and verify:

```powershell
sam --version
```

If `sam` is installed but not recognized in the current terminal session, use full path once:

```powershell
& "C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd" --version
```

If `sam build` fails with `python3.12 on your PATH`, add Python 3.12 to PATH in that session:

```powershell
$env:PATH="C:\Users\<your-user>\AppData\Local\Programs\Python\Python312;C:\Users\<your-user>\AppData\Local\Programs\Python\Python312\Scripts;" + $env:PATH
```

## 4. Backend Deploy (SAM)
From `backend/`:

```bash
sam build
sam deploy --guided
```

Required guided values:
- `AgentRuntimeUrl`: AgentCore runtime endpoint invoke HTTPS URL (not ARN string).
- `template.yaml` is already set with your pool/client values.

After deploy, note `ApiUrl` output.

If deploy fails with `lambda:TagResource` AccessDenied:
- Your IAM user/role running `sam deploy` needs permission to tag Lambda resources during creation.
- Ask admin to add at least these actions for deployer identity:
  - `lambda:CreateFunction`, `lambda:UpdateFunctionCode`, `lambda:UpdateFunctionConfiguration`, `lambda:TagResource`
  - `apigateway:*` (or scoped create/update permissions for HTTP API)
  - `iam:CreateRole`, `iam:AttachRolePolicy`, `iam:PassRole`, `iam:TagRole`
  - `cloudformation:*` (or scoped stack create/update permissions)
  - `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on SAM artifact bucket

Example quick fix policy statement (deployer permissions):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:TagResource",
        "apigateway:*",
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "cloudformation:*",
        "s3:PutObject",
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": "*"
    }
  ]
}
```

After permissions are fixed, run:

```powershell
cd backend
& "C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd" deploy
```

(`samconfig.toml` was already created, so guided prompts are not needed again.)

URL meanings:
- `ApiUrl` (from SAM output) is your public backend API base URL. Set this in frontend env as `VITE_API_BASE_URL`.
- `AgentRuntimeUrl` is internal backend target URL for invoking your AgentCore endpoint.
- Runtime endpoint ARN (`.../runtime-endpoint/DEFAULT`) is not the same as invoke HTTPS URL.

Where to get `AgentRuntimeUrl`:
- Open Bedrock AgentCore -> your runtime -> endpoint `DEFAULT` -> `Test endpoint`.
- If URL is not explicitly shown, open browser DevTools -> Network, send one Test endpoint request, and copy the request URL from that network call.
- Use that as `AgentRuntimeUrl` during `sam deploy --guided`.

## 5. Frontend React Setup
Files are in `frontend-react/`.

React scaffold is already initialized in this repository; no extra `create` command is needed.

Create `.env` from `.env.example`:

```bash
VITE_AWS_REGION=us-east-1
VITE_COGNITO_USER_POOL_ID=us-east-1_l6dPnsMxY
VITE_COGNITO_USER_POOL_CLIENT_ID=4hb4q7nat1kqbm6ehsgtq3e2op
VITE_API_BASE_URL=<API_URL_FROM_SAM_OUTPUT>
```

Test locally:

```bash
cd frontend-react
npm install
npm run dev
```

## 6. Amplify Hosting Deploy
- Push repo to GitHub.
- In Amplify Console: New app -> Host web app -> connect repo.
- App root: `frontend-react`.
- Build command: `npm ci && npm run build`.
- Output dir: `dist`.
- Add environment variables from `.env` values in Amplify Console.

## 7. Logging Consistency
- Runtime already emits `agent_request_trace` as one structured JSON event.
- Filter CloudWatch by `agent_request_trace` to focus only on your custom log schema.
- OTel/platform logs may still exist, but operational queries should target your event field.

## 8. Notes
- Do not expose Cognito app client secret in frontend.
- Frontend uses Amplify Auth session tokens; backend uses API Gateway JWT authorizer.
- Local helper scripts (`login.py`, `invoke.py`, `web_server.py`) and local token/config files are removed to avoid mixed workflows.
