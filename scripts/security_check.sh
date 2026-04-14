#!/bin/bash

# Security Check Script
# Checks tracked source files for likely secret exposure before commit/publish

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
    local exclude_file=${3:-}
    
    echo -n "Checking for $description... "
    
    # Search in source-like files only (exclude generated/runtime paths)
    result=$(find . -type f \( -name "*.md" -o -name "*.py" -o -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.json" -o -name "*.sh" -o -name "*.yml" -o -name "*.yaml" \) \
        ! -path "./.notes/*" \
        ! -path "./node_modules/*" \
        ! -path "./frontend/node_modules/*" \
        ! -path "./venv/*" \
        ! -path "./dist/*" \
        ! -path "./frontend/dist/*" \
        ! -path "./build/*" \
        ! -path "./checkpoints/*" \
        ! -path "./frontend/package-lock.json" \
        ! -path "./frontend/pnpm-lock.yaml" \
        ! -path "./.git/*" \
        ${exclude_file:+! -path "$exclude_file"} \
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
# Targeted patterns first to reduce false positives.
check_pattern "VITE_[A-Z0-9_]*(KEY|TOKEN|SECRET)[A-Z0-9_]*\s*=.*" "Vite secret-like env vars (public by design)"
check_pattern "MOBILE_GATEWAY_API_KEY\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded mobile gateway API keys"
check_pattern "OPENCLAW_API_KEY\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded OpenClaw API keys"
check_pattern "X-OpenClaw-API-Key['\"]?\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded X-OpenClaw-API-Key headers"
check_pattern "Bearer\s+[A-Za-z0-9._-]{20,}" "hardcoded bearer tokens"
check_pattern "[A-Fa-f0-9]{64}" "64-character hex strings (possible API keys)"
# Keep this broad pattern late and require mixed-case/alnum to reduce lockfile noise.
check_pattern "(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{32,}" "long mixed alphanumeric strings (potential API keys)"
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

# Check for suspicious credential assignments/usages, not normal identifier names
check_pattern "credential(s)?\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded credential assignments" "./scripts/security_check.sh"
check_pattern "auth_token\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded auth_token values" "./scripts/security_check.sh"
check_pattern "access_token\s*[:=]\s*['\"][^'\"]+['\"]" "hardcoded access_token values" "./scripts/security_check.sh"

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
    echo "If a secret was already committed, rotate it first, then rewrite git history."
    echo "Consider adding them to .gitignore if they're environment-specific."
    exit 1
fi

