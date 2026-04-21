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

Your user token is a secret key that Stoat stores in your browser when you log in. It works like a temporary password — Ferry uses it to create channels, send messages, and build your server on your behalf.

**You do NOT need to create a bot or register an application.** You are copying a value that already exists in your browser right now (assuming you are logged in to Stoat).

!!! warning "Keep your token private"
    Anyone with your token can act as your Stoat account. Do not paste it into chat messages, screenshots, or text files you share with others. Treat it like a password.

!!! warning "Use your regular Stoat login — not a bot"
    Ferry needs a normal user account (the one you use to chat). Bot tokens cannot create servers and will fail with a "403 Forbidden" error.

### How to find your token

You will be opening a hidden panel in your browser called "developer tools." This sounds technical, but you just need to click a few things — no coding involved.

1. Open your Stoat instance in a **web browser** (Chrome, Edge, or Firefox) and **make sure you are logged in**. You should see your channels and messages.

2. Press **F12** on your keyboard. A panel will split open along the side or bottom of the browser window — this is the developer tools panel. On macOS, press **Option + Command + I** instead.

    <!-- screenshot: browser-devtools-open -->

    !!! tip "Nothing happened when you pressed F12?"
        Some laptops require you to hold the **Fn** key while pressing F12. Try **Fn + F12**. Alternatively, right-click anywhere on the page, choose **Inspect** from the menu, and the panel will open.

3. At the top of the developer tools panel, you will see a row of tabs. Click the one labelled **Application** (in Chrome or Edge). In Firefox, it is called **Storage** instead.

    !!! tip "Can't see the Application tab?"
        If the panel is narrow, some tabs may be hidden. Look for a **>>** button at the end of the tab row — click it to reveal the hidden tabs.

4. In the left sidebar of the Application panel, look for a section called **Local Storage**. Click the small arrow or triangle next to it to expand it. You will see one or more website addresses listed underneath. Click the one that matches your Stoat instance — for example, `https://stoat.chat` or `https://chat.yourdomain.com`.

    <!-- screenshot: devtools-local-storage-expanded -->

5. A table appears on the right side with two columns: **Key** and **Value**. Scroll through the list and find the row where the Key column says **`session_token`** (or something ending in `token`).

6. Click that row. The full value will appear — either in the row itself or in a panel below the table. **Select the entire value and copy it** (right-click → Copy, or Ctrl+C / Cmd+C). This is your token.

    <!-- screenshot: devtools-session-token-selected -->

!!! info "What does a token look like?"
    It is a long string of random-looking letters and numbers, typically 40–64 characters. Example shape (not a real token): `A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0`. If what you copied is very short (under 20 characters), you probably copied the wrong field — go back and look for the longer value.

!!! question "The table is empty — there is nothing to copy?"
    This means your browser has not saved a login session yet. Try these fixes:

    - **Make sure you are actually logged in.** Go to your Stoat instance (e.g. `https://stoat.chat/app`) in the same browser tab and log in. Then go back to the developer tools panel — the table should now have entries.
    - **Refresh the page.** Press F5 or click the refresh button, then check the table again.
    - **Disable browser extensions.** Privacy-focused extensions (uBlock Origin, Privacy Badger, etc.) can block Local Storage. Try disabling them temporarily, or open Stoat in a **private/incognito window** (Ctrl+Shift+N in Chrome, Ctrl+Shift+P in Firefox).
    - **Try a different browser.** If you normally use Firefox, try Chrome (or vice versa). Log in to Stoat there and repeat the steps above.

---

## 3. Should I create the Stoat server first?

You have two options. Neither is wrong — choose whichever fits your situation.

**Option A — Let Ferry create the server for you (recommended)**

Ferry will create a brand-new Stoat server, set up all the channels and roles, and migrate your Discord content into it. You do not need to do anything in Stoat beforehand. This is the simplest path for most admins.

**Option B — Use a server you already created (`--server-id` flag)**

If you have already created an empty server in Stoat and want Ferry to populate it — for example, because you set a custom name and icon manually, or because your migration account is not the one you want to own the server — you can tell Ferry which server to use.

To find your server's ID, open it in Stoat, go to **Server Settings**, and look for a field labelled **Server ID** (it is a short string of letters and numbers, for example `01ABCDEF123`). In the GUI, enter this in the **Existing Server ID** field under Advanced Options. On the command line, pass it with the `--server-id` option.

!!! info "Using `--server-id` with a non-owner account"
    If the account running Ferry did not create the server, make sure it has been given a role with all required permissions before you start. See the next section for the list. Ferry will verify the server is accessible during the CONNECT phase and warn you if it cannot reach it.

---

## 4. Account permissions

The account whose token you use must have the following permissions on the target Stoat server. If Ferry created the server (Option A above), you are the server owner and already have everything.

If you are using an existing server (Option B), create a role with these permissions and assign it to your account before running Ferry:

| Permission | Purpose |
|---|---|
| ManageRole | Required to display original author names with colours |
| ViewChannel | Required to access channels |
| ReadMessageHistory | Required to read existing messages |
| SendMessage | Required to post migrated messages |
| ManageMessages | Required to pin messages |
| SendEmbeds | Required to send rich message embeds |
| UploadFiles | Required to upload attachments |
| Masquerade | Required to show each message under its original Discord author's name and avatar |
| React | Required to add reactions |

!!! warning "There is no single 'Administrator' permission in Stoat"
    Unlike Discord, Stoat does not have a catch-all Administrator permission. You must grant each permission in the list above individually.

!!! note "Emoji migration on existing servers"
    If you are migrating to an existing server (not one Ferry creates), you may also need the **ManageCustomisation** permission to upload custom emoji. This permission is not included in the minimum set above because Ferry can create emoji only on servers it owns outright. If your target server was created by a different account, add ManageCustomisation to the role you assign to the Ferry account.

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
