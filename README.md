# ArtImageHub Backend

FastAPI backend service for AI-powered photo restoration with Stripe subscription management.

## Features

- **AI Photo Restoration**
  - Face enhancement (GFPGAN)
  - Super resolution (Real-ESRGAN)
  - Colorization support
  - Image quality upscaling

- **Payment & Subscription**
  - 7-day free trial with automatic billing
  - Stripe Checkout integration
  - Customer Portal for subscription management
  - Webhook event processing
  - Subscription status tracking

- **Freemium Model**
  - IP-based rate limiting (3 downloads/day for free users)
  - 720p preview with watermark for non-subscribers
  - Unlimited original quality for Pro users

## Tech Stack

- **Framework**: FastAPI 0.129.0
- **Python**: 3.12+
- **Package Manager**: uv
- **Payment**: Stripe 8.0.0
- **AI Services**:
  - Replicate API
  - Gradio Client 2.0.3
- **Image Processing**: Pillow 10.0.0
- **Database**: SQLite (migrate to Turso for production)

## Prerequisites

- Python 3.12 or higher
- uv package manager ([installation guide](https://github.com/astral-sh/uv))
- Stripe account with test/live API keys
- Replicate API token
- Gradio API credentials

## Installation

1. Install dependencies:
```bash
uv sync
```

2. Create `.env` file:
```bash
cp .env.example .env
```

3. Configure environment variables:
```env
# API Keys
REPLICATE_API_TOKEN=your_replicate_token
GRADIO_USERNAME=your_gradio_username
GRADIO_PASSWORD=your_gradio_password

# Stripe Configuration
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PRICE_ID=price_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Frontend URL
FRONTEND_URL=http://localhost:3000
```

## Development

Start the development server:
```bash
uv run uvicorn app.main:app --reload --port 8000
```

API will be available at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **API Base**: http://localhost:8000

## API Endpoints

### Photo Restoration
- `POST /api/restore` - Submit photo for restoration
  - Accepts: multipart/form-data with image file
  - Query params: `face_enhance`, `colorize`, `upscale`
  - Returns: task_id

- `GET /api/tasks/{task_id}` - Check task status
  - Returns: status (pending/processing/completed/failed) + result_url

### Download
- `GET /api/download/check-limit` - Check daily download limit
  - Query params: `email` (optional)
  - Returns: `{allowed, remaining, is_subscriber}`

- `GET /api/download/{task_id}` - Download restored photo
  - Query params: `email` (optional)
  - Returns: Original (subscribers) or 720p preview (free users)

### Payment
- `POST /api/payment/start-trial` - Create Stripe Checkout for trial
  - Body: `{email: string}`
  - Returns: `{checkout_url}`

- `GET /api/payment/subscription/{email}` - Get subscription status
  - Returns: `{email, is_active, status, trial_end, current_period_end, cancel_at_period_end}`

- `POST /api/payment/cancel` - Cancel subscription at period end
  - Body: `{email: string}`
  - Returns: `{message}`

- `POST /api/payment/create-portal-session` - Create Stripe Portal session
  - Body: `{email: string}`
  - Returns: `{url}`

- `GET /api/payment/verify-session/{session_id}` - Verify checkout completion
  - Returns: `{status, email, subscription_status, trial_end}`

- `POST /api/payment/webhook` - Stripe webhook endpoint
  - Handles: checkout.session.completed, customer.subscription.*

## Database Schema

### `subscriptions`
```sql
CREATE TABLE subscriptions (
    email TEXT PRIMARY KEY,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    status TEXT,
    trial_start TEXT,
    trial_end TEXT,
    current_period_start TEXT,
    current_period_end TEXT,
    cancel_at_period_end INTEGER,
    created_at TEXT,
    updated_at TEXT
)
```

### `webhook_events`
```sql
CREATE TABLE webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT,
    processed_at TEXT
)
```

### `downloads`
```sql
CREATE TABLE downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT,
    date TEXT,
    task_id TEXT,
    created_at TEXT
)
```

## Stripe Webhook Setup

1. Install Stripe CLI:
```bash
brew install stripe/stripe-cli/stripe
```

2. Login to Stripe:
```bash
stripe login
```

3. Forward webhooks to local server:
```bash
stripe listen --forward-to localhost:8000/api/payment/webhook
```

4. Copy the webhook signing secret to `.env`:
```env
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Testing

### Manual API Testing
Use the Swagger UI at http://localhost:8000/docs

### Test Stripe Integration
1. Use test credit card: `4242 4242 4242 4242`
2. Any future expiry date (e.g., 12/34)
3. Any 3-digit CVC

### Test Webhook Events
```bash
stripe trigger checkout.session.completed
stripe trigger customer.subscription.created
stripe trigger customer.subscription.deleted
```

## Deployment

### Environment Setup
1. Set production environment variables
2. Update `FRONTEND_URL` to production domain
3. Configure Stripe production keys
4. Set up production webhook endpoint

### Render.com Deployment
1. Create new Web Service
2. Connect GitHub repository
3. Build command: `uv sync`
4. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add environment variables in Render dashboard
6. Add Stripe webhook endpoint: `https://your-app.onrender.com/api/payment/webhook`

### Database Migration
For production, migrate from SQLite to Turso (SQLite at the edge):
1. Create Turso database: `turso db create artimagehub-prod`
2. Update connection string in code
3. Run migrations

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app + CORS setup
│   ├── api/
│   │   ├── restore.py       # Photo restoration endpoints
│   │   ├── download.py      # Download + rate limiting
│   │   └── payment.py       # Stripe integration
│   ├── services/
│   │   ├── ai_service.py    # Replicate + Gradio clients
│   │   └── database.py      # SQLite operations
│   └── config.py            # Environment configuration
├── pyproject.toml           # uv dependencies
├── uv.lock                  # Lock file
└── README.md               # This file
```

## Error Handling

The API returns structured error responses:
```json
{
  "detail": "Error message",
  "error_code": "RATE_LIMIT_EXCEEDED"
}
```

Common HTTP status codes:
- `200` - Success
- `400` - Bad request (invalid input)
- `404` - Resource not found
- `409` - Conflict (e.g., subscription already exists)
- `429` - Rate limit exceeded
- `503` - Service unavailable (Stripe not configured)

## Monitoring

Key metrics to monitor:
- API response times (`/api/restore`, `/api/tasks/*`)
- Stripe webhook success rate
- AI service success rate (Replicate/Gradio)
- Daily active users (unique IPs)
- Trial → Paid conversion rate
- Subscription churn rate

## Security Considerations

- Stripe webhook signature verification enabled
- Rate limiting on download endpoints
- Input validation on all endpoints
- CORS configured for frontend domain only
- Environment variables for sensitive data
- No passwords stored (email-based auth via Stripe)

## Support

For issues or questions:
- Backend bugs: Check FastAPI logs
- Stripe issues: Check Stripe Dashboard logs
- AI service failures: Check Replicate/Gradio status pages

## License

Proprietary - All rights reserved
