# Security Incident Response: Key.pem Exposure

## ✅ Actions Taken
1. ✅ Removed `key.pem` from all Git history using `git filter-branch`
2. ✅ Force pushed cleaned history to GitHub
3. ✅ Added `.gitignore` to prevent future commits
4. ✅ Cleaned local Git cache with `git gc --aggressive`

## ⚠️ Important: Check for Forks

If anyone forked this repository **AFTER** you committed `key.pem`, their fork still has the key in history.

### Check if your repo has any forks:
```bash
# On GitHub: Go to https://github.com/henrycode03/orchestrator
# Look for "Forks" section
```

### If forks exist:
1. **Contact fork owners** and tell them to:
   - Remove the key immediately
   - Delete their fork and re-fork from the cleaned version
   
2. **Or use this script** to help them clean their fork:
   ```bash
   # Run this in their forked repo
   git filter-branch --force --index-filter \
     'git rm --cached --ignore-unmatch frontend/certs/key.pem' \
     --prune-empty --tag-name-filter cat -- --all
   
   git reflog expire --expire=now --all
   git gc --prune=now --aggressive
   
   git push origin main --force
   ```

## 🔒 Next Steps

### 1. Regenerate Certificates (RECOMMENDED)
Even though we removed it from history, there's a small chance:
- Someone already downloaded it
- GitHub's backup might still have it temporarily

**Best practice: Generate new certificates!**

```bash
cd frontend/certs/

# Remove old certificates
rm -f cert.pem key.pem

# Generate new self-signed certificates (for development)
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 365 -nodes -subj "/CN=localhost"
```

### 2. Update Any Config Files That Reference the Old Key
If any configuration files reference the old key, update them.

### 3. Monitor GitHub for Exposure
Check if the key appears in any public searches:
```bash
# Search GitHub API for your key
curl -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/search/code?q=key.pem+repo:henrycode03/orchestrator"
```

## 🛡️ Prevention

### Always add to .gitignore:
```gitignore
# SSL/TLS Certificates (SECURITY - NEVER COMMIT)
*.pem
*.key
cert.pem
key.pem
certs/
```

### Use environment variables for sensitive config:
```bash
# In .env or environment
SSL_KEY_PATH=/path/to/secure/key.pem
SSL_CERT_PATH=/path/to/secure/cert.pem
```

### Consider using:
- **GitHub Secrets** for CI/CD
- **HashiCorp Vault** for production
- **AWS Secrets Manager** for cloud deployments

## 📝 Summary

- ✅ Key removed from Git history
- ✅ Force pushed to GitHub
- ✅ .gitignore added
- ⚠️ Consider regenerating certificates for maximum security
- ⚠️ Check for forks and help them clean their repos
