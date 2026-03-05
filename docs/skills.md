# Skills — Recurring Procedures

## Custom Domain: GoDaddy → Google App Engine

Use this every time a new domain is purchased on GoDaddy and needs to point to a GCP App Engine app.

### Step 1 — Verify domain in GCP
1. GCP Console → App Engine → Settings → Custom Domains → Add
2. Select or verify the domain (Google will send a TXT record to add in GoDaddy)
3. GCP shows you the DNS records to add (A, AAAA, CNAME)

### Step 2 — In GoDaddy DNS (example: crab.travel)

**DELETE these GoDaddy defaults:**
- A record `@` → `WebsiteBuilder Site` (GoDaddy's placeholder — remove it)
- CNAME `www` → anything pointing back to GoDaddy (e.g. `crab.travel.`) — replace it

**ADD these records for App Engine:**

| Type | Name | Data |
|------|------|------|
| A | @ | 216.239.32.21 |
| A | @ | 216.239.34.21 |
| A | @ | 216.239.36.21 |
| A | @ | 216.239.38.21 |
| AAAA | @ | 2001:4860:4802:32::15 |
| AAAA | @ | 2001:4860:4802:34::15 |
| AAAA | @ | 2001:4860:4802:36::15 |
| AAAA | @ | 2001:4860:4802:38::15 |
| CNAME | www | ghs.googlehosted.com |

**KEEP these (don't touch):**
- NS records (GoDaddy nameservers — never delete)
- SOA record
- TXT `@` → google-site-verification=... (needed for GCP ownership verification)
- TXT `_dmarc` (email security)
- CNAME `_domainconnect` (GoDaddy internal)

### Step 3 — Back in GCP
- Click Done in the custom domain wizard
- SSL certificate auto-provisions (takes a few minutes)
- DNS propagation: up to 24 hours, usually <1 hour

### Notes
- GoDaddy does NOT support ALIAS/ANAME records — use the 4 A records above instead
- If GoDaddy shows a warning about multiple A records for @, that's fine — App Engine needs all 4
- The AAAA records are IPv6 — optional but recommended, add them if GoDaddy allows

---

## Deploy to App Engine
```bash
python gcloud_deploy.py
```
- Auto-verifies correct GCP project
- Deploys new version
- Cleans up old versions
- Live at https://crab.travel
