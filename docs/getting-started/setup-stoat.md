# Setting Up Your Stoat Account

Before running Ferry, you need two pieces of information: the **API URL** for your Stoat instance (the address Ferry uses to talk to Stoat) and your **user token** (a secret key that proves you are logged in).

This page walks you through finding both.

---

## 1. Find your Stoat API URL

The API URL is the web address that Ferry sends data to. It is different from the address you visit in your browser to use the Stoat chat interface.

=== "Official Stoat (stoat.chat)"

    Your API URL is:

    ```
    https://api.stoat.chat
    ```

    You can copy this exactly as written — no changes needed.

=== "Self-hosted Stoat"

    1. Your API URL is usually `https://api.yourdomain.com`, where `yourdomain.com` is the domain you used when setting up Stoat. For example, if your Stoat instance is at `https://chat.example.com`, the API is typically at `https://api.example.com`.

    2. If you are not sure, check the configuration file you used during setup. In a Docker Compose setup, look for the service labelled `api` or `backend` and the domain assigned to it in your reverse proxy config (Caddy, Nginx, Traefik, etc.).

    3. To confirm the URL is correct, paste it into your browser's address bar and press Enter. A working API endpoint returns a short block of text (JSON — machine-readable data) rather than a blank page or error. It should look something like:

        ```json
        {"revolt":"0.7.x","features":{...},"ws":"...","app":"..."}
        ```

    <!-- screenshot: api-url-browser-json-response -->

    !!! tip "Still not sure which URL to use?"
        Search your Docker Compose file or reverse proxy config for `REVOLT_PUBLIC_URL` or the hostname assigned to the `api` container. That is your API URL.

---

## 2. Get your Stoat user token

Your user token is a long string of characters that acts like a temporary password for the Stoat API. Ferry needs it to create channels, send messages, and build your server on your behalf.

!!! warning "Keep your token private"
    Anyone with your token can take actions as your account. Do not paste it into chat messages, screenshots, or text files you share with others. Treat it like a password.

!!! warning "Use a regular user account, not a bot account"
    Ferry must create the Stoat server on your behalf. The Stoat API does not allow bot tokens to create servers — they receive a "403 Forbidden" error. You must use the token from a regular user account (the kind you log into in your browser).

Follow these steps to find your token:

1. Open your Stoat instance in a web browser and log into the account you want Ferry to use.

2. Press **F12** on your keyboard to open the browser developer tools (a panel that opens alongside the page). On macOS, you can also use **Option + Command + I**.

    <!-- screenshot: browser-devtools-open -->

3. Click the **Application** tab at the top of the developer tools panel (in Chrome or Edge). If you are using Firefox, look for the **Storage** tab instead.

    !!! tip "Can't find the Application tab?"
        The tabs along the top of the developer tools may be cut off if the panel is narrow. Look for a `>>` or `+` button at the end of the tab row to see hidden tabs.

4. In the left sidebar of the Application (or Storage) panel, expand the **Local Storage** section and click on your Stoat domain. For example: `https://app.stoat.chat` or `https://chat.yourdomain.com`.

    <!-- screenshot: devtools-local-storage-expanded -->

5. A table of key-value pairs appears on the right. Look for a row where the **Key** column says `session_token` (or a similar name ending in `token`).

6. Click that row, then click the value in the **Value** column and copy the entire string. This is your token.

    <!-- screenshot: devtools-session-token-selected -->

!!! info "The token will be a long string"
    User tokens are typically 40–64 characters long and contain a mix of letters and numbers. If what you copied looks very short (under 20 characters), you may have copied the wrong field — go back and look for the longer value.

---

## 3. Should I create the Stoat server first?

You have two options. Neither is wrong — choose whichever fits your situation.

**Option A — Let Ferry create the server for you (recommended)**

Ferry will create a brand-new Stoat server, set up all the channels and roles, and migrate your Discord content into it. You do not need to do anything in Stoat beforehand. This is the simplest path for most admins.

**Option B — Use a server you already created (`--server-id` flag)**

If you have already created an empty server in Stoat and want Ferry to populate it — for example, because you set a custom name and icon manually, or because your migration account is not the one you want to own the server — you can tell Ferry which server to use.

To find your server's ID, open it in Stoat, go to **Server Settings**, and look for a field labelled **Server ID** (it is a short alphanumeric string). Pass it to Ferry with the `--server-id` option.

!!! info "Using `--server-id` with a non-owner account"
    If the account running Ferry did not create the server, make sure it has been given a role with all required permissions before you start. See the next section for the list.

---

## 4. Account permissions

The account whose token you use must have the following permissions on the target Stoat server. If Ferry created the server (Option A above), you are the server owner and already have everything.

If you are using an existing server (Option B), create a role with these permissions and assign it to your account before running Ferry:

| Permission | Purpose |
|---|---|
| ManageRole | Required for masquerade (username spoofing for author names) |
| ViewChannel | Required to access channels |
| ReadMessageHistory | Required to read existing messages |
| SendMessage | Required to post migrated messages |
| ManageMessages | Required to pin messages |
| SendEmbeds | Required to send rich message embeds |
| UploadFiles | Required to upload attachments |
| Masquerade | Required to set per-message author name and avatar |
| React | Required to add reactions |

!!! warning "There is no single 'Administrator' permission in Stoat"
    Unlike Discord, Stoat does not have a catch-all Administrator permission. You must grant each permission in the list above individually.

---

## 5. Raising limits on self-hosted instances

If your Discord server is large — many channels, lots of custom emoji, or long messages — the default Stoat limits may be too low. You can raise them before running the migration.

!!! info "This only applies to self-hosted Stoat"
    If you are using the official stoat.chat service, you cannot change these limits yourself. Contact Stoat support if you hit a limit.

Open your `Revolt.overrides.toml` file (yes, it is still named after the old project name) and add or adjust the following values:

```toml
[limits.global]
server_channels = 500   # default is 200; raise if your Discord has many threads
server_emoji = 200      # default is 100; raise if you have lots of custom emoji
message_length = 4000   # default is 2000; raise if you have very long messages
```

Restart your Stoat instance after saving the file for the changes to take effect.

!!! tip "Not sure where `Revolt.overrides.toml` is?"
    It is typically in the same directory as your `docker-compose.yml` file, or in the config volume mounted into the `api` container. Check the documentation for your specific Stoat setup guide.

---

Once you have your API URL and token, you are ready to run Ferry. Head to [Your First Migration](first-migration.md) for next steps.
