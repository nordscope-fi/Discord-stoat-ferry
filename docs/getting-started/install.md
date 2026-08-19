# Installing Discord Ferry

Discord Ferry runs entirely on your own computer. There is no account to create and no data is sent to any external service — your messages stay between you and your Stoat server.

Pick your operating system below to get started.

---

=== "Windows"

    1. Go to the [Discord Ferry releases page](https://github.com/nordscope-fi/Discord-stoat-ferry/releases) on GitHub.
    2. Under the latest release, click **Ferry-windows-x86_64.exe** to download it.
    3. Double-click the downloaded file to run it. Ferry opens its own window.

    !!! info "No browser opens"
        The downloaded app draws its own window and does not launch your browser. If the window
        stays blank or does not appear, open `http://localhost:8765` in any browser while Ferry
        is running. That address is fully supported and gives you the same interface.

    !!! tip "The app also takes commands"
        `Ferry-windows-x86_64.exe --help` lists every command, and `--version` reports the build.
        See the [CLI Reference](../guides/cli-reference.md) for what each command does.

    <!-- screenshot: windows-smartscreen-warning -->

    !!! warning "Windows SmartScreen warning"
        Windows may show a blue dialog saying **"Windows protected your PC"**. This happens because Ferry is not signed with a paid code-signing certificate — a common situation for open-source tools.

        To proceed:

        1. Click **More info** (the small link below the warning text).
        2. Click **Run anyway**.

        Ferry is safe to run. You can review the full source code on GitHub.

=== "macOS"

    First, check which Mac you have. Click the Apple menu in the top-left corner and choose **About This Mac**:

    | What you see | Download this file |
    |---|---|
    | **Chip:** Apple M1 / M2 / M3 / M4 | `Ferry-macos-arm64.zip` |
    | **Processor:** Intel | `Ferry-macos-x86_64.zip` |

    Then:

    1. Go to the [Discord Ferry releases page](https://github.com/nordscope-fi/Discord-stoat-ferry/releases) on GitHub.
    2. Under the latest release, click the file that matches your Mac.
    3. Unzip the downloaded file.
    4. Drag the **Ferry.app** icon into your **Applications** folder.
    5. Double-click **Ferry.app**. macOS blocks it the first time. This is expected — keep reading.

    <!-- screenshot: macos-gatekeeper-warning -->

    !!! danger "Click Done, not Move to Bin"
        macOS shows a dialog reading **"Apple could not verify 'Ferry' is free of malware that may harm your Mac or compromise your privacy."** It offers two buttons: **Done** and **Move to Bin**.

        Click **Done**. The highlighted **Move to Bin** button deletes Ferry.

    !!! warning "Approving Ferry (once)"
        Ferry is not notarized through Apple's paid developer program, so macOS asks you to approve it by hand the first time:

        1. Open **System Settings** from the Apple menu.
        2. Go to **Privacy & Security** and scroll down to the **Security** section.
        3. You will see **"Ferry" was blocked to protect your Mac**. Click **Open Anyway**.
        4. Click **Open Anyway** once more to confirm, then enter your Mac password.

        Ferry opens. From now on you can launch it with a normal double-click.

    !!! info "No browser opens"
        Ferry.app draws its own window and does not launch your browser. If the window stays
        blank or does not appear, open `http://localhost:8765` in any browser while Ferry is
        running. That address is fully supported and gives you the same interface.

        **Not seeing the button?** It only appears *after* macOS has blocked Ferry, and it disappears again after about an hour. Double-click **Ferry.app** once more, then go straight back to **Privacy & Security**.

    !!! tip "Faster, if you are comfortable with Terminal"
        This single command clears the download quarantine flag, which stops macOS blocking the app in the first place:

        ```bash
        xattr -dr com.apple.quarantine /Applications/Ferry.app
        ```

        Then open Ferry normally. The source code is publicly available on GitHub if you would like to check it first.

=== "Linux"

    Ferry is distributed as a Python package on Linux. You will need a terminal (the text command window — search "Terminal" in your applications) and **Python 3.11 or newer**.

    1.  Install **pipx** (a tool for safely installing Python programs) if you do not have it already:

        - Debian / Ubuntu:
          ```bash
          sudo apt install pipx
          ```
        - Fedora:
          ```bash
          sudo dnf install pipx
          ```
        - Arch / Manjaro:
          ```bash
          sudo pacman -S python-pipx
          ```

    2.  Install Ferry:
        ```bash
        pipx install discord-ferry
        ```

    3.  Verify the installation worked:
        ```bash
        ferry --help
        ```

    4.  Launch the graphical interface (opens in your browser):
        ```bash
        ferry-gui
        ```

    !!! info "No desktop app on Linux"
        Installed this way, Ferry opens in your web browser and does not draw its own window.
        The downloaded Windows and macOS apps bundle the window toolkit; a `pipx` install leaves
        it out unless you ask for it with `pipx install "discord-ferry[native]"`. Everything
        else behaves identically.

---

## Troubleshooting

**Antivirus blocks Ferry.exe (Windows)**

PyInstaller (the packaging tool used to create Ferry's single `.exe` file) can trigger false positives in some antivirus programs. The file is safe. Add `Ferry.exe` to your antivirus exclusions list, then try running it again. If you are unsure how to do this, search for "add exclusion" along with the name of your antivirus software.

**macOS says "Ferry is damaged and can't be opened"**

This sometimes happens if macOS quarantined the file during download. Open **Terminal** (search for it in Spotlight) and run:

```bash
xattr -dr com.apple.quarantine /Applications/Ferry.app
```

Then try opening Ferry again. The `-r` is important: without it the flag is only cleared from the folder, and the app inside stays blocked.

**macOS says it "could not verify Ferry is free of malware"**

This is the normal first-launch prompt, not a fault. Follow the **Approving Ferry** steps in the macOS tab above. Whatever you do, do not click **Move to Bin** — that deletes the app.

**"Python not found" or "python3: command not found" (Linux)**

Ferry requires Python 3.11 or newer. Check your version:

```bash
python3 --version
```

If the version shown is below 3.11, or the command is not found, install a newer Python using your distribution's package manager. For example, on Ubuntu:

```bash
sudo apt install python3.11
```

!!! tip "Still stuck?"
    Open an issue on the [Discord Ferry GitHub page](https://github.com/nordscope-fi/Discord-stoat-ferry/issues) and include the error message you saw. Someone from the community will help you out.
