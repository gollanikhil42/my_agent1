# PHILIPS SENSEI

Professional log analysis and AI chat platform powered by AWS Lambda and Amazon Bedrock.


This is an intelligent log analysis platform designed for DevOps and SRE teams. It combines AWS CloudWatch Logs and X-Ray tracing data with AI-powered analysis to help engineers quickly identify and resolve issues. The platform offers two distinct modes:

1. **Fleet Window Analysis**: Analyze historical logs and traces across multiple sessions within a configurable timeframe (e.g., last 30 minutes, 1 hour, 7 days)
2. **Single Trace Analysis**: Deep-dive investigation of specific distributed traces using X-Ray Trace IDs

Beyond log analysis, PHILIPS SENSEI also serves as a general-purpose AI assistant for asking questions and getting intelligent responses powered by Claude 3.5 Sonnet.

**Key Features:**
- 🔍 **Intelligent Log Parsing**: Automatically extracts relevant information from CloudWatch logs and X-Ray traces
- 💬 **Conversational Interface**: Natural language setup flow guides users through analysis configuration
- 📊 **Automatic Pagination**: Handles large result sets with smart aggregation
- 🎯 **Contextual Analysis**: Maintains conversation history for informed responses
- 🔐 **Enterprise Security**: AWS Cognito-based federated authentication
- 🎨 **Professional UI**: Responsive React interface optimized for desktop use

## Overview

Dual-mode conversational interface for real-time log analysis and general-purpose AI assistance. The Analyzer Chat mode provides intelligent investigation of X-Ray traces and CloudWatch logs with conversational setup flow, while Assistant Chat supplies general-purpose AI support.

**Capabilities:**
- Conversational setup for Fleet Window (multi-session) and Single Trace analysis modes
- Automatic pagination and metric aggregation for large result sets
- Smart session context preservation across conversation history
- Professional React 18 UI with responsive design
- AWS Cognito federated authentication

## Technology Stack

- **Frontend**: React 18+ with vanilla CSS
- **Backend**: AWS Lambda (Python)
- **AI**: Amazon Bedrock (Claude models)
- **Data Sources**: CloudWatch Logs, AWS X-Ray
- **Auth**: AWS Cognito + Amplify
- **Persistence**: DynamoDB, S3

## Quick Start

### Frontend Setup

```bash
cd frontend-react
npm install

cat > .env.local << EOF
VITE_API_BASE_URL=https://your-api-gateway-url/api
VITE_COGNITO_CLIENT_ID=your-cognito-client-id
VITE_COGNITO_DOMAIN=your-cognito-domain.auth.region.amazoncognito.com
VITE_COGNITO_REDIRECT_URI=http://localhost:5173/callback
EOF

npm run dev
```

### Backend Setup

```bash
cd backend/lambda
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sam build && sam deploy --guided
```

## Commands

### Frontend
```bash
npm run dev         # Start dev server
npm run build       # Production build
npm run preview     # Preview build
```

### Backend
```bash
sam build           # Package Lambda
sam deploy --guided # Deploy to AWS
```

## Environment Variables

### Frontend (.env.local)
| Variable | Example |
|----------|---------|
| `VITE_API_BASE_URL` | `https://abc123.execute-api.us-east-1.amazonaws.com/api` |
| `VITE_COGNITO_CLIENT_ID` | `1a2b3c4d5e6f7g8h9i0j` |
| `VITE_COGNITO_DOMAIN` | `yourapp.auth.us-east-1.amazoncognito.com` |
| `VITE_COGNITO_REDIRECT_URI` | `http://localhost:5173/callback` |

### Backend (Lambda)
| Variable | Description |
|----------|-------------|
| `BEDROCK_REGION` | AWS region for Bedrock |
| `CLOUDWATCH_LOGS_ROLE` | IAM role for log access |
| `DYNAMODB_TABLE` | Session persistence table |

## Troubleshooting

### Build Issues
```bash
rm -rf node_modules package-lock.json
npm install
npm run build
```

### Authentication Errors
- Check `.env.local` credentials
- Refresh page to re-authenticate
- Verify JWT token not expired

### CORS Errors
- Verify `VITE_API_BASE_URL`
- Enable CORS on API Gateway
- Check headers in requests

## Documentation

For detailed information, see the [docs/](docs/) directory:

- **[docs/WORKFLOW.md](docs/WORKFLOW.md)** - Detailed workflow, modes, and architecture
- **[docs/KT_DEMO_SCRIPT.md](docs/KT_DEMO_SCRIPT.md)** - Demo script for knowledge transfer
- **[docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md)** - Deployment instructions
- **[docs/STRUCTURE.md](docs/STRUCTURE.md)** - Project structure and organization
- **[docs/ANALYSER_PROMPTS.md](docs/ANALYSER_PROMPTS.md)** - Analyzer prompt templates

## Support

- **Issues**: GitHub Issues
- **Support**: support@philips.com

## License

MIT
