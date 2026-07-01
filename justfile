# Triage development tasks

# Show available commands
default:
    @just --list

# List the project docs (plain Markdown, rendered on GitHub — start at docs/README.md)
docs:
    @echo "Greenfield docs are plain Markdown — start at docs/README.md:" && ls docs/*.md && echo "ADRs:" && ls docs/adr/*.md

# Serve the dashboard app — read views + write surface (set TRIAGE_REGISTRY_URL to enable POST routes)
serve PORT="8000":
    uv run uvicorn triage.dashboard.app:app --host 127.0.0.1 --port {{PORT}}

# Run alembic commands (e.g., just alembic upgrade head)
alembic *ARGS:
    PYTHONPATH=src uv run alembic \
        -c src/triage/component/results_schema/alembic.ini \
        -x db_config_file=database.yaml \
        {{ARGS}}

# Run registry-DB alembic commands (e.g., just alembic-registry upgrade head)
alembic-registry *ARGS:
    PYTHONPATH=src uv run alembic \
        -c src/triage/component/registry_schema/alembic.ini \
        -x db_config_file=database.yaml \
        {{ARGS}}

# Run tests
test *ARGS:
    uv run pytest {{ARGS}}

# Run tests with coverage
test-cov:
    uv run pytest --cov=triage

# Run linter
lint:
    uv run ruff check src/

# Run type checker
typecheck:
    uv run basedpyright

# Sync dependencies
sync *EXTRA:
    uv sync {{EXTRA}}

# Install with dev dependencies
install:
    uv sync --extra dev

# Run triage CLI
triage *ARGS:
    uv run triage {{ARGS}}

# Launch the interactive TUI
tui:
    uv run triage tui

# Start DirtyDuck tutorial database
tutorial-up:
    docker compose -f dirtyduck/docker-compose.yml up -d food_db

# Stop DirtyDuck tutorial
tutorial-down:
    docker compose -f dirtyduck/docker-compose.yml stop

# Launch DirtyDuck bastion shell
tutorial-shell:
    docker compose -f dirtyduck/docker-compose.yml run --service-ports --rm bastion

# Build DirtyDuck images
tutorial-build:
    docker compose -f dirtyduck/docker-compose.yml build

# Rebuild DirtyDuck images (no cache)
tutorial-rebuild:
    docker compose -f dirtyduck/docker-compose.yml build --no-cache

# Show DirtyDuck container status
tutorial-status:
    docker compose -f dirtyduck/docker-compose.yml ps

# View DirtyDuck logs
tutorial-logs:
    docker compose -f dirtyduck/docker-compose.yml logs -f -t

# Clean up DirtyDuck resources (removes containers, images, volumes)
tutorial-clean:
    docker compose -f dirtyduck/docker-compose.yml down --rmi all --remove-orphans --volumes

# Start DonorsChoose (KDD Cup 2014) EWS tutorial database (build + up)
donors-up:
    docker compose -f donorschoose/docker-compose.yml up -d --build donors_db

# psql into the DonorsChoose database
donors-shell:
    docker compose -f donorschoose/docker-compose.yml exec donors_db psql -U donors_user -d donors

# Stop DonorsChoose
donors-down:
    docker compose -f donorschoose/docker-compose.yml stop

# Clean up DonorsChoose resources (containers, images, volumes — frees the baked data)
donors-clean:
    docker compose -f donorschoose/docker-compose.yml down --rmi all --remove-orphans --volumes

# Start Chicago 311 Service Requests EWS tutorial database (build + up)
chi311-up:
    docker compose -f chicago311/docker-compose.yml up -d --build chi311_db

# psql into the Chicago 311 database
chi311-shell:
    docker compose -f chicago311/docker-compose.yml exec chi311_db psql -U chi311_user -d chi311

# Stop Chicago 311
chi311-down:
    docker compose -f chicago311/docker-compose.yml stop

# Clean up Chicago 311 resources (containers, images, volumes — frees the baked data)
chi311-clean:
    docker compose -f chicago311/docker-compose.yml down --rmi all --remove-orphans --volumes
