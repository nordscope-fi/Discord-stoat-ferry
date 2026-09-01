# Operate the Ferry feedback service in Coolify

This page is for the maintainer deploying and recovering Ferry's public feedback service. It keeps the service inside the named Personal Apps environment, proves both health boundaries, and protects its short-lived private data. Keep it separate.

## Create one isolated application in Personal Apps

Open the [Personal Apps environment](https://coolify.nordscope.fi/project/b4cc0ckccogsk4go80ock0o0/environment/zkw48cw40gc48088888s44g4). Confirm the intended Hetzner server before creating a resource. Check the server name. Do not reuse an application, database, volume, credentials, or monitoring resource from another project.

Create one application from `nordscope-fi/Discord-stoat-ferry` with these settings:

- Build from `services/feedback/Dockerfile` on the released branch.
- Route `https://feedback.nordscope.fi` through Coolify's HTTPS proxy to container port 8080.
- Attach one persistent private volume at `/data`.
- Exclude that volume from every operational backup.
- Keep the container's numeric non-root user and built-in health check unchanged.

The dedicated GitHub App must be installed only on `nordscope-fi/Discord-stoat-ferry`. It needs read access to repository metadata and write access to Issues and Discussions. Do not grant access to another repository.

## Set the runtime fields in Coolify

Create these application environment fields:

- `FERRY_FEEDBACK_REPOSITORY`
- `FERRY_FEEDBACK_GITHUB_APP_ID`
- `FERRY_FEEDBACK_GITHUB_INSTALLATION_ID`
- `FERRY_FEEDBACK_GITHUB_PRIVATE_KEY`
- `FERRY_FEEDBACK_DATABASE_PATH`
- `FERRY_FEEDBACK_CHALLENGE_KEY`
- `FERRY_FEEDBACK_SOURCE_HASH_KEY`
- `FERRY_FEEDBACK_CONTACT_KEY`
- `FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS`

Mark the App key and the three service keys as secret values. Each service key must decode to 32 bytes, and all three must differ. The database path must name a file under `/data`. Set the trusted proxy field to the canonical private network used by Coolify's immediate proxy. Do not use a catch-all network.

Deploy only after the volume and fields are present. The service refuses to start when a required field is missing or unsafe. Stop on failure.

## Check local storage and GitHub readiness

The container health check calls `/health`, which proves that the process can query SQLite. Check it over the public route too:

```bash
curl --fail --silent https://feedback.nordscope.fi/health
```

The dependency check at `/ready` proves the GitHub App installation, repository, permission levels, Bug labels, and both Discussion categories. It returns only `ready` or `unready`, without internal identifiers.

```bash
curl --fail --silent https://feedback.nordscope.fi/ready
```

Both commands must return HTTP 200 before a release uses the service.

Check both.

## Read or delete an optional contact email

Run operator commands inside the application container. Use the receipt identifier recorded in the private service event.

```bash
python -m discord_ferry.feedback_service \
  --database /data/feedback.db contact show RECEIPT_ID
```

Delete the contact promptly. Use:

```bash
python -m discord_ferry.feedback_service \
  --database /data/feedback.db contact delete RECEIPT_ID
```

The contact command reads the encryption key from `FERRY_FEEDBACK_CONTACT_KEY`. Contact is encrypted and expires within 30 days. Receipt metadata expires within 7 days, and quota rows expire within 24 hours.

## Resolve a pending GitHub receipt without a second post

A timeout after a GitHub write leaves the receipt pending. First inspect GitHub for the exact receipt marker. If the matching Issue or Discussion exists, bind its URL locally:

```bash
python -m discord_ferry.feedback_service \
  --database /data/feedback.db receipt resolve RECEIPT_ID GITHUB_URL
```

If the full GitHub search proves that no matching destination exists, mark the pending receipt absent:

```bash
python -m discord_ferry.feedback_service \
  --database /data/feedback.db receipt absent RECEIPT_ID
```

Do not mark a receipt absent merely because the first search page has no match. The next user-chosen retry performs reconciliation and must never create a second public item for an uncertain write.

## Rotate keys with their different data costs in mind

Rotate the GitHub App private key in GitHub and Coolify together, then redeploy and check `/ready`.

Changing `FERRY_FEEDBACK_CHALLENGE_KEY` invalidates outstanding 15-minute challenges. Changing `FERRY_FEEDBACK_SOURCE_HASH_KEY` starts fresh quota hash buckets. Schedule either change when that short disruption is acceptable.

Changing `FERRY_FEEDBACK_CONTACT_KEY` makes existing retained contact unreadable. Keep the old key until those contacts expire or are deleted. If compromise requires an immediate rotation, accept the loss of unread contact, replace the key, and delete the affected contact rows by receipt.

## Audit logs without copying private data

Coolify logs should contain only JSON metadata fields for event, receipt, state, destination kind, response class, and duration. They must not contain report text, diagnostics, contact email, network source, request path, query, headers, or credentials.

After a controlled smoke run, search the exported service log for the exact smoke description, diagnostic, and contact markers. Every search must return no match. Do not paste a credential into a search command or an issue comment.

## Roll back without losing receipt history

Redeploy the last known good image or commit in the same Coolify application. Keep the same environment fields and `/data` volume. A second application or empty database would lose pending reconciliation and duplicate protection.

After rollback, require HTTP 200 from both `/health` and `/ready`. If either fails, leave public feedback unavailable until the same application is healthy rather than routing writes to an unproven replacement.

## Remove every production smoke item

The guarded smoke command creates one marked Bug Issue, one Ideas Discussion, and one General Discussion. Record all three returned URLs, verify their labels, categories, marker, and Ferry provenance, then remove them.

The dedicated GitHub App can remove the two Discussions. A maintainer identity must remove the test Issue. The smoke run is incomplete until repository search finds no marked item and the log audit finds none of the submitted content.
