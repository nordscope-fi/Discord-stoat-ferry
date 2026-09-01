"""In-app feedback action and dialog for Ferry's web interface."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from nicegui import app, ui

from discord_ferry import __version__
from discord_ferry.core.security import sanitize_secrets_for_public_output
from discord_ferry.feedback import (
    FeedbackClient,
    FeedbackDiagnostics,
    FeedbackDraft,
    FeedbackInterface,
    FeedbackKind,
    FeedbackServiceError,
    FeedbackValidationError,
    RuntimeContext,
    build_diagnostics,
    render_diagnostics,
)

_SUBMISSION_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    FeedbackServiceError,
    FeedbackValidationError,
)
_STEPS = ("1. Report", "2. Diagnostics", "3. Public review", "4. Result")


@dataclass(slots=True)
class FeedbackGuiContext:
    """Small, mutable view context owned by one rendered page."""

    interface: FeedbackInterface
    stage: str
    last_error: str | None
    log_path: Path | None


async def _pick_save_path() -> Path | None:
    """Ask the native desktop shell where to save a recovery draft."""

    native = getattr(app, "native", None)
    window = getattr(native, "main_window", None)
    if window is None:
        ui.notify("Saving a draft requires the desktop app.", type="warning")
        return None

    import webview  # Optional native dependency.

    dialog_type = getattr(webview.FileDialog, "SAVE", None)
    if dialog_type is None:
        dialog_type = webview.SAVE_DIALOG
    selection = window.create_file_dialog(
        dialog_type,
        save_filename="discord-ferry-feedback.md",
        file_types=("Markdown files (*.md)", "All files (*.*)"),
    )
    if not selection:
        return None
    return Path(selection[0] if isinstance(selection, (list, tuple)) else selection)


class _FeedbackDialogController:
    """Own one page-local feedback draft and its modal workflow."""

    def __init__(self, context: FeedbackGuiContext) -> None:
        self.context = context
        self.client = ui.context.client
        self.stage = 0
        self.kind_value = FeedbackKind.GENERAL.value
        self.description_value = ""
        self.expected_value = ""
        self.reproduction_value = ""
        self.contact_value = ""
        self.include_diagnostics = False
        self.last_error_value = ""
        self.log_excerpt_value = ""
        self.draft: FeedbackDraft | None = None
        self.submission_task: asyncio.Task[None] | None = None
        self.result_url: str | None = None
        self.result_error: str | None = None
        self.reconnect_failure_pending = False

        with (
            ui.dialog().props("persistent") as self.dialog,
            ui.card().classes(
                "w-[46rem] max-w-[94vw] max-h-[92vh] p-0 overflow-hidden bg-slate-50"
            ),
        ):
            with (
                ui.element("div").classes("w-full bg-slate-900 px-6 py-5"),
                ui.row().classes("w-full items-start justify-between gap-4"),
            ):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("rate_review", color="amber-5").classes("text-2xl")
                    with ui.column().classes("gap-0"):
                        ui.label("Share feedback").classes("text-xl font-semibold text-white")
                        ui.label("Review every public detail before it leaves Ferry.").classes(
                            "text-sm text-slate-300"
                        )
                ui.button(icon="close", on_click=self.close).props(
                    "flat round color=grey-4 aria-label='Close feedback'"
                )
            self.content = ui.column().classes("w-full gap-5 px-6 py-5 overflow-y-auto")

        self.button = (
            ui.button("Feedback", icon="rate_review", on_click=self.open)
            .props("outline color=amber-8 no-caps aria-label='Share feedback'")
            .classes("mt-4 font-medium")
        )
        self.client.on_disconnect(self._handle_disconnect)
        self.client.on_connect(self._handle_reconnect)

    def open(self) -> None:
        if self.draft is None and not self.description_value and self.context.last_error:
            self.kind_value = FeedbackKind.BUG.value
            self.include_diagnostics = True
            self.last_error_value = self.context.last_error
        self._render()
        self.dialog.open()

    def close(self) -> None:
        if self.submission_task is not None and not self.submission_task.done():
            self.submission_task.cancel()
        self.submission_task = None
        self.content.clear()
        self.dialog.close()

    def _handle_disconnect(self) -> None:
        if self.submission_task is None or self.submission_task.done():
            return
        self.submission_task.cancel()
        self.submission_task = None
        self.result_url = None
        self.result_error = "The connection closed before Ferry received a result."
        self.reconnect_failure_pending = True

    def _handle_reconnect(self) -> None:
        if not self.reconnect_failure_pending:
            return
        self.reconnect_failure_pending = False
        self.stage = 3
        self._render()
        self.dialog.open()

    def _render(self) -> None:
        with self.client, self.content:
            self.content.clear()
            self._render_steps()
            if self.stage == 0:
                self._render_report()
            elif self.stage == 1:
                self._render_diagnostics()
            elif self.stage == 2:
                self._render_review()
            else:
                self._render_result()

    def _render_steps(self) -> None:
        with ui.row().classes("w-full gap-2 flex-wrap"):
            for index, label in enumerate(_STEPS):
                colour = "amber-8" if index == self.stage else "blue-grey-3"
                text_colour = "white" if index == self.stage else "blue-grey-9"
                ui.badge(label, color=colour, text_color=text_colour).props("rounded")

    def _render_report(self) -> None:
        ui.label("What should we know?").classes("text-lg font-semibold text-slate-900")
        ui.label(
            "Your report becomes a public GitHub Issue or Discussion. "
            "The contact email is sent privately to the Ferry maintainers."
        ).classes("text-sm text-slate-600")
        kind = ui.select(
            {
                FeedbackKind.BUG.value: "Bug",
                FeedbackKind.IDEA.value: "Idea",
                FeedbackKind.GENERAL.value: "General feedback",
            },
            value=self.kind_value,
            label="Type",
        ).classes("w-full")
        description = (
            ui.textarea("Description", value=self.description_value)
            .props("outlined autogrow")
            .classes("w-full")
        )
        expected = (
            ui.textarea("Expected result", value=self.expected_value)
            .props("outlined autogrow")
            .classes("w-full")
        )
        reproduction = (
            ui.textarea("Reproduction steps", value=self.reproduction_value)
            .props("outlined autogrow")
            .classes("w-full")
        )
        contact = (
            ui.input("Private contact email", value=self.contact_value)
            .props("outlined type=email")
            .classes("w-full")
        )
        with ui.row().classes("w-full justify-end"):
            ui.button(
                "Continue to diagnostics",
                icon="arrow_forward",
                on_click=lambda: self._continue_report(
                    kind.value,
                    description.value,
                    expected.value,
                    reproduction.value,
                    contact.value,
                ),
            ).props("color=amber-8 no-caps")

    def _continue_report(
        self,
        kind: object,
        description: object,
        expected: object,
        reproduction: object,
        contact: object,
    ) -> None:
        try:
            kind_value = FeedbackKind(str(kind))
            description_value = str(description or "")
            expected_value = str(expected or "")
            reproduction_value = str(reproduction or "")
            contact_value = str(contact or "")
            if self.draft is None:
                self.draft = FeedbackDraft(
                    kind=kind_value,
                    description=description_value,
                    expected=expected_value,
                    reproduction=reproduction_value,
                    contact_email=contact_value,
                )
            else:
                self.draft.edit_kind(kind_value)
                self.draft.edit_description(description_value)
                self.draft.edit_expected(expected_value)
                self.draft.edit_reproduction(reproduction_value)
                self.draft.edit_contact_email(contact_value)
        except FeedbackValidationError as exc:
            ui.notify(str(exc), type="negative")
            return

        self.kind_value = kind_value.value
        self.description_value = description_value
        self.expected_value = expected_value
        self.reproduction_value = reproduction_value
        self.contact_value = contact_value
        self.stage = 1
        self._render()

    def _runtime_context(self) -> RuntimeContext:
        return RuntimeContext(
            ferry_version=__version__,
            operating_system=sys.platform,
            architecture=platform.machine(),
            interface=self.context.interface.value,
            stage=self.context.stage,
            last_error=self.context.last_error,
        )

    def _diagnostic_preview(self) -> FeedbackDiagnostics:
        automatic = build_diagnostics(
            self._runtime_context(),
            include_logs=True,
            log_path=self.context.log_path,
        )
        if not self.last_error_value and automatic.last_error:
            self.last_error_value = automatic.last_error
        if not self.log_excerpt_value and automatic.log_excerpt:
            self.log_excerpt_value = automatic.log_excerpt
        return FeedbackDiagnostics.from_mapping(
            {
                **automatic.to_mapping(),
                "last_error": self.last_error_value or None,
                "log_excerpt": self.log_excerpt_value or None,
            }
        )

    def _render_diagnostics(self) -> None:
        ui.label("Choose what diagnostic context to include").classes(
            "text-lg font-semibold text-slate-900"
        )
        ui.label(
            "Diagnostics are optional and public. Ferry removes known credential patterns, "
            "but names or message content may remain. Read and edit the preview."
        ).classes("text-sm text-amber-900 bg-amber-100 border border-amber-300 rounded p-3")
        include = ui.checkbox("Include diagnostics", value=self.include_diagnostics).props(
            "color=amber-8"
        )

        try:
            preview = self._diagnostic_preview()
            preview_text = render_diagnostics(preview)
        except FeedbackValidationError as exc:
            preview_text = f"Diagnostic preview unavailable: {exc}"

        ui.label("Automatic context").classes("font-medium text-slate-800")
        ui.label(preview_text).classes(
            "w-full whitespace-pre-wrap rounded bg-slate-900 p-4 text-sm text-slate-100"
        )
        last_error = (
            ui.textarea("Last error", value=self.last_error_value)
            .props("outlined autogrow")
            .classes("w-full")
        )
        log_excerpt = (
            ui.textarea("Recent log text", value=self.log_excerpt_value)
            .props("outlined autogrow")
            .classes("w-full")
        )
        acknowledgement = ui.checkbox(
            "I understand the diagnostics will be public", value=False
        ).props("color=amber-8")
        with ui.row().classes("w-full justify-between gap-2"):
            ui.button("Back", on_click=self._back_to_report).props("flat no-caps")
            ui.button(
                "Continue to public review",
                icon="arrow_forward",
                on_click=lambda: self._continue_diagnostics(
                    include.value,
                    last_error.value,
                    log_excerpt.value,
                    acknowledgement.value,
                ),
            ).props("color=amber-8 no-caps")

    def _back_to_report(self) -> None:
        self.stage = 0
        self._render()

    def _continue_diagnostics(
        self,
        include: object,
        last_error: object,
        log_excerpt: object,
        acknowledged: object,
    ) -> None:
        assert self.draft is not None
        self.include_diagnostics = bool(include)
        self.last_error_value = str(last_error or "")
        self.log_excerpt_value = str(log_excerpt or "")
        try:
            if self.include_diagnostics:
                if not bool(acknowledged):
                    raise FeedbackValidationError(
                        "Confirm that the diagnostic preview may be public"
                    )
                diagnostics = self._diagnostic_preview()
                self.draft.edit_diagnostics(diagnostics)
                self.draft.acknowledge_diagnostics()
            else:
                self.draft.edit_diagnostics(None)
        except FeedbackValidationError as exc:
            ui.notify(str(exc), type="negative")
            return
        self.stage = 2
        self._render()

    def _render_review(self) -> None:
        assert self.draft is not None
        ui.label("Review the exact public report").classes("text-lg font-semibold text-slate-900")
        ui.label(
            "Only the text below will be public. Your contact email is not shown here."
        ).classes("text-sm text-slate-600")
        ui.label(self.draft.render_public_body()).classes(
            "w-full max-h-80 overflow-y-auto whitespace-pre-wrap rounded "
            "border border-slate-300 bg-white p-4 text-sm text-slate-900"
        )
        public_acknowledgement = ui.checkbox(
            "I understand this report will be public on GitHub", value=False
        ).props("color=amber-8")
        with ui.row().classes("w-full justify-between gap-2"):
            ui.button("Edit report", icon="edit", on_click=self._back_to_report).props(
                "flat no-caps"
            )
            ui.button(
                "Send feedback",
                icon="send",
                on_click=lambda: self._send(public_acknowledgement.value),
            ).props("color=amber-8 no-caps")

    def _send(self, public_acknowledged: object) -> None:
        assert self.draft is not None
        if not bool(public_acknowledged):
            ui.notify("Confirm that the report may be public", type="negative")
            return
        self.draft.acknowledge_public()
        self.stage = 3
        self.result_url = None
        self.result_error = None
        self._render()
        self.submission_task = asyncio.create_task(self._submit())

    async def _submit(self) -> None:
        assert self.draft is not None
        try:
            async with FeedbackClient() as feedback_client:
                receipt = await feedback_client.submit(self.draft)
        except _SUBMISSION_ERRORS as exc:
            self.result_error = sanitize_secrets_for_public_output(str(exc))
            if not self.result_error:
                self.result_error = "The feedback service did not accept the report."
        else:
            self.result_url = receipt.url
        finally:
            self.submission_task = None

        with contextlib.suppress(Exception):
            self._render()

    def _render_result(self) -> None:
        if self.result_url is not None:
            ui.icon("check_circle", color="green-7").classes("text-4xl")
            ui.label("Feedback shared").classes("text-xl font-semibold text-slate-900")
            ui.link(self.result_url, self.result_url, new_tab=True).classes(
                "break-all text-blue-700"
            )
        elif self.result_error is not None:
            ui.icon("cloud_off", color="red-7").classes("text-4xl")
            ui.label("Feedback could not be sent").classes("text-xl font-semibold text-slate-900")
            ui.label(self.result_error).classes("text-sm text-red-800")
            ui.label(
                "Your draft is still here. Try again when you choose, or keep a local copy."
            ).classes("text-sm text-slate-600")
            assert self.draft is not None
            ui.label("Preserved public draft").classes("font-medium text-slate-800")
            ui.label(self.draft.render_public_body()).classes(
                "w-full max-h-56 overflow-y-auto whitespace-pre-wrap rounded "
                "border border-slate-300 bg-white p-4 text-sm text-slate-900"
            )
            with ui.row().classes("w-full flex-wrap gap-2"):
                ui.button("Retry", icon="refresh", on_click=lambda: self._send(True)).props(
                    "color=amber-8 no-caps"
                )
                ui.button("Copy public draft", icon="content_copy", on_click=self._copy).props(
                    "outline no-caps"
                )
                ui.button("Save draft", icon="save", on_click=self._save).props("outline no-caps")
                ui.button("Edit report", icon="edit", on_click=self._back_to_report).props(
                    "flat no-caps"
                )
        else:
            ui.spinner("dots", size="lg", color="amber-8")
            ui.label("Sending feedback").classes("text-lg font-semibold text-slate-900")
            ui.label("This can take a few seconds.").classes("text-sm text-slate-600")

        with ui.row().classes("w-full justify-end"):
            ui.button("Close feedback", on_click=self.close).props("flat no-caps")

    def _copy(self) -> None:
        assert self.draft is not None
        ui.clipboard.write(self.draft.copy_text())
        ui.notify("Public draft copied", type="positive")

    async def _save(self) -> None:
        assert self.draft is not None
        path = await _pick_save_path()
        if path is None:
            return
        try:
            self.draft.save(path)
        except OSError as exc:
            ui.notify(f"Could not save draft: {exc}", type="negative")
            return
        ui.notify("Draft saved", type="positive")


def render_feedback_action(context: FeedbackGuiContext) -> ui.button:
    """Render one page-local feedback action and its four-stage dialog."""

    return _FeedbackDialogController(context).button


__all__ = ["FeedbackGuiContext", "render_feedback_action"]
