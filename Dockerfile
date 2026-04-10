FROM python:3.12-slim

WORKDIR /app

# Install uv for dependency management.
# Avoid external multi-stage pulls from ghcr.io to reduce build flakiness on Render.
RUN pip install --no-cache-dir uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application
COPY . .

# Create storage directories
RUN mkdir -p uploads results

# Expose port
EXPOSE 8000

# Run with uvicorn
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
