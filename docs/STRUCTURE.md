# Project Structure Documentation

This document outlines the PHILIPS SENSEI project structure following industry best practices.

## Current Directory Layout

```
agentcore-project/
│
├── docs/                                    # All project documentation
│   ├── README.md                           # Project overview and quick start
│   ├── WORKFLOW.md                         # Detailed workflow and mode documentation
│   ├── KT_DEMO_SCRIPT.md                   # KT session demonstration script
│   ├── DEPLOYMENT_GUIDE.md                 # Deployment instructions
│   ├── ANALYSER_PROMPTS.md                 # Prompt templates for analyzer
│   ├── DELIVERY_SUMMARY.md                 # Delivery notes
│   ├── COMPONENT_INTEGRATION_GUIDE.md      # Component integration guide
│   ├── COMPONENT_QUICK_REFERENCE.md        # Quick component reference
│   ├── STRUCTURE.md                        # This file
│   └── system_understanding.md             # System architecture
│
├── frontend-react/                         # React frontend application
│   ├── src/
│   │   ├── components/                     # Reusable React components
│   │   │   ├── AnalyzerSetup/              # Analyzer mode selection interface
│   │   │   ├── ChatBubble/                 # Message bubble component
│   │   │   ├── ChatInput/                  # Legacy input component (deprecated)
│   │   │   ├── ChatSuggestions/            # Legacy suggestions (deprecated)
│   │   │   ├── Composer/                   # Text input and suggestions
│   │   │   ├── HelpPanel/                  # Help documentation panel
│   │   │   ├── MessageList/                # Chat message list
│   │   │   └── Topbar/                     # Application header
│   │   ├── services/                       # API and AWS service calls
│   │   │   └── (api.js would go here)
│   │   ├── utils/                          # Helper functions
│   │   │   └── (helpers would go here)
│   │   ├── hooks/                          # Custom React hooks (future)
│   │   ├── styles/                         # Shared CSS files
│   │   ├── App.jsx                         # Main application component
│   │   ├── main.jsx                        # Vite entry point
│   │   ├── awsConfig.js                    # AWS Amplify configuration
│   │   └── styles.css                      # Global styles
│   ├── public/                              # Static assets
│   ├── dist/                                # Build output (generated)
│   ├── package.json                         # NPM dependencies
│   ├── vite.config.js                       # Vite build configuration
│   ├── index.html                           # HTML template
│   ├── .env                                 # Local environment variables (git-ignored)
│   ├── .env.example                         # Environment template
│   └── README.md                            # Frontend-specific documentation
│
├── backend/                                 # Lambda backend
│   ├── src/
│   │   ├── handlers/                        # Lambda event handlers
│   │   │   ├── chat_handler.py              # General chat endpoint
│   │   │   ├── analysis_endpoint.py         # Log analysis endpoint
│   │   │   └── __init__.py
│   │   ├── services/                        # Business logic layer
│   │   │   ├── bedrock_service.py           # Amazon Bedrock integration
│   │   │   ├── logs_service.py              # CloudWatch Logs service
│   │   │   ├── xray_service.py              # X-Ray service
│   │   │   └── __init__.py
│   │   ├── utils/                           # Shared utilities
│   │   │   ├── parsers.py                   # Response parsers
│   │   │   ├── validators.py                # Input validation
│   │   │   └── __init__.py
│   │   └── __init__.py                      # Package initialization
│   ├── requirements.txt                     # Python dependencies
│   ├── template.yaml                        # SAM template for deployment
│   ├── samconfig.toml                       # SAM configuration
│   ├── .env                                 # Local Lambda environment
│   ├── .env.example                         # Environment template
│   ├── .aws-sam/                            # SAM build artifacts (generated)
│   └── lambda/                              # Legacy: Old flat structure (deprecated)
│
├── config/                                  # Configuration files
│   └── .env.example                         # Environment variables template
│
├── scripts/                                 # Deployment and utility scripts
│   ├── deploy_agent.ps1                     # PowerShell deployment script
│   ├── deploy.sh                            # Bash deployment script (future)
│   ├── setup.sh                             # Setup script (future)
│   └── agentcore-observability-deployer-policy.json
│
├── .bedrock_agentcore/                      # Bedrock agent configuration (generated)
├── .venv/                                   # Python virtual environment (git-ignored)
├── .git/                                    # Git repository
│
├── .gitignore                               # Git ignore rules
├── .env                                     # Root environment variables (git-ignored)
├── .env.example                             # Root environment template
├── .dockerignore                            # Docker ignore rules
├── amplify.yml                              # AWS Amplify configuration
│
├── pricing_catalog_working.json             # Reference data
├── my_agent1.py                             # Legacy test script (to be archived)
├── requirements.txt                         # Legacy root requirements (to be removed)
│
└── README.md                                # Root: Brief project overview

```

## Directory Organization Principles

### 1. **Separation of Concerns**
- Frontend code in `frontend-react/`
- Backend code in `backend/src/`
- Each has its own dependencies and configuration

### 2. **Scalable Component Structure**
- Each component in its own folder (e.g., `components/Topbar/`)
- Keeps related files together
- Easy to locate and test

### 3. **Service Layer Pattern**
- API calls in `services/` (frontend)
- Business logic in `services/` (backend)
- Keeps components and handlers thin
- Easy to mock for testing

### 4. **Documentation Centralization**
- All docs in `docs/` directory
- README at root for quick reference
- Specific guides in subdirectories

### 5. **Configuration Management**
- Environment-specific configs in `config/`
- `.env` files for local development (git-ignored)
- `.env.example` as template

## File Naming Conventions

### Frontend React
- **Components**: PascalCase (e.g., `Topbar.jsx`, `MessageList.jsx`)
- **Hooks**: camelCase with `use` prefix (e.g., `useAuth.js`, `useFetch.js`)
- **Services**: camelCase (e.g., `apiService.js`, `authService.js`)
- **Utils**: camelCase (e.g., `helpers.js`, `validators.js`)
- **Styles**: kebab-case (e.g., `styles.css`, `message-list.css`)

### Backend Python
- **Modules**: snake_case (e.g., `bedrock_service.py`, `logs_service.py`)
- **Classes**: PascalCase (e.g., `BedrockService`, `LogsService`)
- **Functions**: snake_case (e.g., `get_logs()`, `parse_response()`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `API_BASE_URL`, `MAX_RETRIES`)

## Import Path Examples

### Frontend
```javascript
// Component usage
import { Topbar } from "../components/Topbar/Topbar";
import { useAuth } from "../hooks/useAuth";
import { apiService } from "../services/apiService";
import { formatDate } from "../utils/helpers";
```

### Backend
```python
# Handler imports
from src.services.bedrock_service import BedrockService
from src.utils.validators import validate_input
from src.utils.parsers import parse_logs
```

## Future Improvements

1. **Add Tests**: Create `__tests__/` or `tests/` directories
2. **Add Hooks**: Create `frontend-react/src/hooks/` for custom hooks
3. **Add Context**: Create `frontend-react/src/context/` for React context
4. **Add Types**: Create `frontend-react/src/types/` for TypeScript types
5. **CI/CD**: Add `.github/workflows/` for GitHub Actions
6. **Docker**: Add Dockerfile and docker-compose.yml

## Migration Checklist

### Phase 1: Documentation Organization ✓
- [x] Create `docs/` directory
- [ ] Move markdown files to `docs/`
- [ ] Update root README.md with brief overview
- [ ] Create STRUCTURE.md documenting organization

### Phase 2: Backend Reorganization (Optional)
- [ ] Create `backend/src/handlers/`, `services/`, `utils/`
- [ ] Move `lambda/*.py` files to appropriate folders
- [ ] Update imports in all backend files
- [ ] Test deployment

### Phase 3: Frontend Services (Optional)
- [ ] Create `frontend-react/src/services/`
- [ ] Create `frontend-react/src/utils/`
- [ ] Extract API calls to services
- [ ] Extract helpers to utils
- [ ] Update imports in App.jsx

### Phase 4: Configuration Organization (Optional)
- [ ] Create `config/` directory
- [ ] Move config files to `config/`
- [ ] Create `.env.example` templates
- [ ] Update gitignore

## Key Changes from Previous Structure

| Old Location | New Location | Reason |
|---|---|---|
| Root/*.md | docs/*.md | Centralized documentation |
| Root/.env | config/.env | Grouped with other configs |
| backend/lambda/*.py | backend/src/handlers/*.py | Clearer structure |
| (None) | backend/src/services/ | Added service layer |
| (None) | frontend-react/src/services/ | Added for API calls |
| (None) | frontend-react/src/utils/ | Added for helpers |

## Backwards Compatibility

All import paths remain unchanged during Phase 1 (documentation only). 
Future phases will include migration guides for existing code.

## References

- [Node/Frontend Project Best Practices](https://github.com/goldbergyoni/nodebestpractices)
- [Python Project Structure](https://docs.python-guide.org/writing/structure/)
- [Create React App Project Structure](https://create-react-app.dev/)
