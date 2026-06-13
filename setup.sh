#!/bin/bash
# setup.sh — One-command project setup
# Run: bash setup.sh

set -e
echo "================================================"
echo "  Trading System — Setup"
echo "================================================"

# Python version check
python3 --version || { echo "Python 3.11+ required"; exit 1; }

# Create virtualenv
echo "Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Copy env file
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — fill in your API keys"
fi

# Create log directory
mkdir -p logs models db

echo ""
echo "================================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. source venv/bin/activate"
echo "  3. python bot.py --paper   (start paper trading)"
echo "================================================"
