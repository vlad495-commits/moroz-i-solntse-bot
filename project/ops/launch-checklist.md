# Launch Checklist

Every unchecked item blocks launch.

- [ ] TLS works on the client domain and HTTP redirects safely.
- [ ] rotated secrets are present only in server `.env`.
- [ ] TOTP is enabled for owner/admin accounts.
- [ ] YCLIENTS tokens, company ID, services, staff and booking permissions are verified.
- [ ] restore drill completed against an isolated database.
- [ ] alerts route to technical and business recipients.
- [ ] evals meet the approved threshold.
- [ ] load gate passes for 30 inbound/min and 20 active chats.
- [ ] staff knows manual escalation and outage procedure.
- [ ] legal texts and consent copy are approved.
- [ ] smoke.ps1 passes after deploy.
- [ ] rollback runbook has a known previous image.
