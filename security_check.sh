#!/bin/bash

# Security Check Script
# Checks all public (non-.notes/) files for sensitive information

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo "🔒 Security Check - Looking for sensitive information..."
echo ""

ERRORS=0

# Function to check for patterns
check_pattern() {
    local pattern=$1
    local description=$2
    
    echo -n "Checking for $description... "
    
    # Search in public files only (exclude .notes/, node_modules/, venv/)
    result=$(find . -type f \( -name "*.md" -o -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.json" \) \
        ! -path "./.notes/*" \
        ! -path "./node_modules/*" \
        ! -path "./venv/*" \
        ! -path "./.git/*" \
        -exec grep -lE "$pattern" {} \; 2>/dev/null | grep -v "^$" || true)
    
    if [ -n "$result" ]; then
        echo -e "${RED}FOUND${NC}"
        echo "$result" | while read file; do
            echo -e "  ${YELLOW}⚠️  $file${NC}"
            grep -nE "$pattern" "$file" 2>/dev/null | head -3 | while read line; do
                echo "    $line"
            done
        done
        ERRORS=$((ERRORS + 1))
    else
        echo -e "${GREEN}✓ Clean${NC}"
    fi
}

# Check for API keys and tokens
check_pattern "[A-Za-z0-9]{32,}" "long alphanumeric strings (potential API keys)"
check_pattern "sk-[A-Za-z0-9]{20,}" "sk- prefixed tokens (Stripe/OpenAI)"
check_pattern "ghp_[A-Za-z0-9]{36}" "GitHub Personal Access Tokens"
check_pattern "gho_[A-Za-z0-9]{36}" "GitHub OAuth Tokens"
check_pattern "ghu_[A-Za-z0-9]{36}" "GitHub User Tokens"
check_pattern "ghs_[A-Za-z0-9]{36}" "GitHub Server Tokens"

# Check for passwords and secrets
check_pattern "password\s*=\s*['\"][^'\"]+['\"]" "hardcoded passwords"
check_pattern "secret\s*=\s*['\"][^'\"]+['\"]" "hardcoded secrets"
check_pattern "api_key\s*=\s*['\"][^'\"]+['\"]" "hardcoded API keys"

# Check for specific IP addresses (container networks)
check_pattern "172\.\d+\.\d+\.\d+" "172.x.x.x IP addresses (Docker containers)"
check_pattern "192\.168\.\d+\.\d+" "192.168.x.x IP addresses (private networks)"
check_pattern "10\.\d+\.\d+\.\d+" "10.x.x.x IP addresses (private networks)"

# Check for credentials
check_pattern "credential" "credential references"
check_pattern "auth_token" "auth_token references"
check_pattern "access_token" "access_token references"

echo ""
echo "========================================"

if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}✓ All checks passed! No sensitive information found.${NC}"
    echo ""
    echo "Public files are safe to commit to GitHub."
    exit 0
else
    echo -e "${RED}⚠️  Found $ERRORS potential security issues!${NC}"
    echo ""
    echo "Review the files above and remove or sanitize sensitive information."
    echo "Consider adding them to .gitignore if they're environment-specific."
    exit 1
fi
