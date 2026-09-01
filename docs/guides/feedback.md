# Share feedback without leaving Ferry

This page is for Ferry users who want to report a bug, suggest an idea, or send general feedback. You can review the full public report and send it without a GitHub account. Nothing is sent before you confirm.

## Start from the app or the command line

Choose one of three report types:

- **Bug** creates a public GitHub Issue with the `bug` and `triage` labels.
- **Idea** creates a public GitHub Discussion in the Ideas category.
- **General feedback** creates a public GitHub Discussion in the General category.

In the app, select **Feedback** on the setup, validation, or migration screen. From a terminal, run:

```bash
ferry feedback
```

Neither route needs a GitHub account. A short description is the only report field you must write. Expected results and reproduction steps are optional.

## Review what will be public before sending

Ferry shows the exact public preview before it sends anything. Read it, edit any field that needs work, then confirm that the report may be public on GitHub.

Diagnostics are optional and need a separate confirmation. They can include your Ferry version, operating system, processor type, interface, current stage, last visible error, and up to 100 recent log lines. Ferry removes known credential patterns, but names or message content may remain. Edit the diagnostic preview or leave it out if it contains anything you do not want to publish.

Ferry does not send raw Discord exports or file attachments through this feedback route.

## Add private contact details only when you want a reply

A contact email is optional. It is not included in the GitHub Issue or Discussion. The feedback service encrypts it and keeps it for no more than 30 days so a maintainer can reply.

The service keeps delivery receipt details for no more than 7 days and keyed network-source quota records for no more than 24 hours. It does not store the source network address. Its private data volume is excluded from operational backups, so expired or deleted contact details do not remain in a backup.

## Keep your draft when sending fails

Ferry never retries a feedback write by itself. After a failure, you decide whether to retry, edit, copy, or save the draft.

The app saves only the public draft with owner-only file permissions. The command-line flow asks before adding a private contact email to a local saved copy. Check the path before saving on a shared computer.

When sending succeeds, Ferry shows the public GitHub Issue or Discussion URL. Open that link to follow replies and updates.
