# Installing Discord Ferry

Discord Ferry runs entirely on your own computer. There is no account to create and no data is sent to any external service — your messages stay between you and your Stoat server.

Pick your operating system below to get started.

---

=== "Windows"

    1. Go to the [Discord Ferry releases page](https://github.com/psthubhorizon/Discord-stoat-ferry/releases) on GitHub.
    2. Under the latest release, click **Ferry.exe** to download it.
    3. Double-click **Ferry.exe** to run it. Your browser will open automatically.

    <!-- screenshot: windows-smartscreen-warning -->

    !!! warning "Windows SmartScreen warning"
        Windows may show a blue dialog saying **"Windows protected your PC"**. This happens because Ferry is not signed with a paid code-signing certificate — a common situation for open-source tools.

        To proceed:

        1. Click **More info** (the small link below the warning text).
        2. Click **Run anyway**.

        Ferry is safe to run. You can review the full source code on GitHub.

=== "macOS"

    1. Go to the [Discord Ferry releases page](https://github.com/psthubhorizon/Discord-stoat-ferry/releases) on GitHub.
    2. Under the latest release, click **Ferry-macos-arm64.zip** to download it.
    3. Unzip the downloaded file.
    4. Drag the **Ferry.app** icon into your **Applications** folder.

    !!! warning "First launch — do NOT double-click"
        The first time you open Ferry, you must right-click (or Control-click) the app icon and select **Open**. If you double-click instead, macOS will refuse to run it.

    <!-- screenshot: macos-gatekeeper-warning -->

    !!! warning "Gatekeeper warning"
        macOS may show a dialog saying **"Ferry can't be opened because Apple cannot check it for malicious software."**

        Click **Open** to proceed. This message appears because Ferry is not notarized through Apple's paid developer program. The source code is publicly available on GitHub.

        After the first launch, you can open Ferry normally by double-clicking it.

=== "Linux"

    Ferry is distributed as a Python package on Linux. You will need a terminal and **Python 3.10 or newer**.

    1.  Install **pipx** if you do not have it already:

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
        On Linux, Ferry opens in your web browser instead of a separate application window. This is normal — it works the same as the Windows and macOS versions.

---

## Troubleshooting

**Antivirus blocks Ferry.exe (Windows)**

PyInstaller, the tool used to package Ferry into a single `.exe` file, can trigger false positives in some antivirus programs. The file is safe. Add `Ferry.exe` to your antivirus exclusions list, then try running it again. If you are unsure how to do this, search for "add exclusion" along with the name of your antivirus software.

**macOS says "Ferry is damaged and can't be opened"**

This sometimes happens if macOS quarantined the file during download. Open **Terminal** (search for it in Spotlight) and run:

```bash
xattr -d com.apple.quarantine /Applications/Ferry.app
```

Then try opening Ferry again.

**"Python not found" or "python3: command not found" (Linux)**

Ferry requires Python 3.10 or newer. Check your version:

```bash
python3 --version
```

If the version shown is below 3.10, or the command is not found, install a newer Python using your distribution's package manager. For example, on Ubuntu:

```bash
sudo apt install python3.11
```

!!! tip "Still stuck?"
    Open an issue on the [Discord Ferry GitHub page](https://github.com/psthubhorizon/Discord-stoat-ferry/issues) and include the error message you saw. Someone from the community will help you out.
