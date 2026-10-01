"""Independent Codex GUI settings and dispatch; Claude settings stay in gui.py."""
import copy
import json

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                            QFormLayout, QHBoxLayout, QLabel, QMessageBox,
                            QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from codex_watcher import CodexWatcher, clean_config

MODEL_LEVELS = ["", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
EFFORT_LEVELS = ["", "low", "medium", "high", "xhigh", "max", "ultra"]


class CodexGuiMixin:
    def _init_cli_gui(self):
        self._watch_targets = "claude"
        self._cli_rows = {"claude": [], "codex": []}
        self._cli_running = {"claude": False, "codex": False}
        self._watch_requested = False
        self._codex_config = clean_config({})

    def _build_cli_controls(self, root):
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Watch"))
        self.watch_targets_combo = QComboBox()
        for label, key in [("Claude CLI only", "claude"), ("Codex CLI only", "codex"),
                           ("Claude + Codex CLI", "both")]:
            self.watch_targets_combo.addItem(label, key)
        self.watch_targets_combo.currentIndexChanged.connect(self._on_watch_targets)
        bar.addWidget(self.watch_targets_combo)
        bar.addWidget(QLabel("Start / Stop applies to the selected CLIs"))
        bar.addStretch()
        root.addLayout(bar)
        self.codex_controls = QWidget()
        controls = QHBoxLayout(self.codex_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Codex CLI"))
        self._codex_spins = {}
        for key, label, low, high in (("poll", "Poll", 1, 3600), ("buffer", "Buffer", 0, 600),
                                     ("retry", "Retry", 5, 3600)):
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setSuffix(" s")
            spin.setValue(self._codex_config[key])
            spin.setToolTip(f"Codex CLI {label.lower()} interval" if key != "buffer" else "Codex CLI extra delay after the usage reset")
            spin.valueChanged.connect(lambda value, k=key: self._on_codex_timing(k, value))
            self._codex_spins[key] = spin
            controls.addWidget(QLabel(label))
            controls.addWidget(spin)
        self.codex_dry_run_check = QCheckBox("Dry-run")
        self.codex_dry_run_check.setToolTip("Codex CLI: detect and log without pressing keys")
        self.codex_dry_run_check.toggled.connect(lambda value: self._on_codex_timing("dry_run", value))
        controls.addWidget(self.codex_dry_run_check)
        self.codex_settings_btn = QPushButton("Codex Advanced…")
        self.codex_settings_btn.clicked.connect(self._open_codex_settings)
        controls.addWidget(self.codex_settings_btn)
        controls.addStretch()
        root.addWidget(self.codex_controls)
        self._update_cli_controls()

    def _update_cli_controls(self):
        selected = self._selected_clis()
        for widget in (self.interval_spin, self.buffer_spin, self.retry_spin, self.dry_run_check):
            widget.setEnabled("claude" in selected)
        self.codex_controls.setVisible("codex" in selected)

    def _sync_codex_controls(self):
        for key, spin in self._codex_spins.items():
            spin.blockSignals(True)
            spin.setValue(self._codex_config[key])
            spin.blockSignals(False)
        self.codex_dry_run_check.blockSignals(True)
        self.codex_dry_run_check.setChecked(self._codex_config["dry_run"])
        self.codex_dry_run_check.blockSignals(False)

    def _on_codex_timing(self, key, value):
        self._codex_config[key] = value
        self._apply_codex_config()

    def _build_codex_worker(self):
        # One UIA thread serializes keyboard operations. The watchers have
        # separate objects, timers, states, settings and drivers.
        self.codex_worker = CodexWatcher()
        self.codex_worker.moveToThread(self.worker_thread)
        self.sig_codex_start.connect(self.codex_worker.start)
        self.sig_codex_stop.connect(self.codex_worker.stop)
        self.sig_codex_config.connect(self.codex_worker.configure)
        self.sig_codex_fire.connect(self.codex_worker.fire_now)
        self.sig_codex_skip.connect(self.codex_worker.skip)
        self.sig_codex_cooldown.connect(self.codex_worker.clear_cooldown)
        self.codex_worker.snapshot.connect(self._on_codex_snapshot)
        self.codex_worker.running_changed.connect(self._on_codex_running)
        self.codex_worker.log.connect(self._append_log)
        self.codex_worker.loops_spent.connect(self._on_codex_loops_spent)

    def _selected_clis(self):
        return ("claude", "codex") if self._watch_targets == "both" else (self._watch_targets,)

    def _start_selected_watchers(self):
        self._watch_requested = True
        selected = self._selected_clis()
        (self.sig_claude_start if "claude" in selected else self.sig_claude_stop).emit()
        (self.sig_codex_start if "codex" in selected else self.sig_codex_stop).emit()

    def _stop_watchers(self):
        self._watch_requested = False
        self.sig_claude_stop.emit()
        self.sig_codex_stop.emit()

    def _on_watch_targets(self, _index):
        self._watch_targets = self.watch_targets_combo.currentData()
        self._update_cli_controls()
        if self._watch_requested:
            self._start_selected_watchers()
        self._merge_cli_rows()
        self._save_settings()

    def _on_claude_snapshot(self, rows):
        self._cli_rows["claude"] = [dict(r, provider="claude") for r in rows]
        self._merge_cli_rows()

    def _on_codex_snapshot(self, rows):
        self._cli_rows["codex"] = rows
        self._merge_cli_rows()

    def _merge_cli_rows(self):
        rows = [r for provider in self._selected_clis() for r in self._cli_rows[provider]]
        self._on_snapshot(rows)

    def _on_claude_running(self, running):
        self._cli_running["claude"] = running
        self._on_running_changed(any(self._cli_running.values()))

    def _on_codex_running(self, running):
        self._cli_running["codex"] = running
        self._on_running_changed(any(self._cli_running.values()))

    def _load_cli_settings(self):
        target = self.settings.value("watch_targets", "claude", type=str)
        self._watch_targets = target if target in ("claude", "codex", "both") else "claude"
        self.watch_targets_combo.blockSignals(True)
        self.watch_targets_combo.setCurrentIndex(self.watch_targets_combo.findData(self._watch_targets))
        self.watch_targets_combo.blockSignals(False)
        try:
            raw = json.loads(self.settings.value("codex_config", "{}", type=str))
        except (ValueError, TypeError):
            raw = {}
        self._codex_config = clean_config(raw)
        self._sync_codex_controls()
        self._update_cli_controls()
        self.sig_codex_config.emit(copy.deepcopy(self._codex_config))

    def _save_cli_settings(self):
        self.settings.setValue("watch_targets", self._watch_targets)
        self.settings.setValue("codex_config", json.dumps(self._codex_config))

    def _apply_codex_config(self):
        self._codex_config = clean_config(self._codex_config)
        self._sync_codex_controls()
        self.sig_codex_config.emit(copy.deepcopy(self._codex_config))
        # Codex changes write only Codex keys; no Claude settings are saved.
        self._save_cli_settings()
        self._last_render_sig = None
        self._render_table()

    def _open_codex_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Codex CLI — settings")
        layout = QVBoxLayout(dlg)
        hint = QLabel("These settings apply only to Codex CLI. Use a separate Windows Terminal window for each session.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        fields = {}
        for key, label, low, high in [("poll", "Poll", 1, 3600), ("buffer", "Buffer after reset", 0, 600),
                                      ("retry", "Network retry", 5, 3600), ("max_retries", "Maximum retries", 1, 100)]:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(self._codex_config[key])
            if key != "max_retries":
                spin.setSuffix(" s")
            fields[key] = spin
            form.addRow(label, spin)
        dry = QCheckBox("Detect and log without pressing keys")
        dry.setChecked(self._codex_config["dry_run"])
        form.addRow("Dry-run", dry)
        layout.addLayout(form)
        note = QLabel("Model / Effort, Exclude and After finish are set per window in the table. Permission and choice dialogs remain waiting for your input.")
        note.setWordWrap(True)
        layout.addWidget(note)
        reset = QPushButton("Reset Codex settings")
        def reset_codex():
            if QMessageBox.question(dlg, "Reset Codex", "Reset Codex timings, exclusions, model / effort and after-finish settings?") != QMessageBox.StandardButton.Yes:
                return
            self._codex_config = clean_config({})
            self._apply_codex_config()
            dlg.reject()
        reset.clicked.connect(reset_codex)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._codex_config.update({key: spin.value() for key, spin in fields.items()})
            self._codex_config["dry_run"] = dry.isChecked()
            self._apply_codex_config()

    def _cli_action(self, row, action):
        codex = row.get("provider") == "codex"
        if action == "fire":
            (self.sig_codex_fire if codex else self.sig_fire_now).emit(row["hwnd"])
        elif action == "skip":
            (self.sig_codex_skip if codex else self.sig_skip).emit(row["hwnd"])
        elif action == "cooldown":
            (self.sig_codex_cooldown if codex else self.sig_clear_cooldown).emit(row["hwnd"])
        elif action == "after_finish":
            (self._on_codex_after_finish if codex else self._on_after_finish_clicked)(row["title"])
        elif action in ("exclude", "include"):
            if codex:
                excluded = self._codex_config["excluded"]
                if action == "exclude" and row["title"] not in excluded:
                    excluded.append(row["title"])
                elif action == "include" and row["title"] in excluded:
                    excluded.remove(row["title"])
                self._apply_codex_config()
            elif action == "exclude":
                self._do_exclude(row["hwnd"], row["title"])
            else:
                self._do_unexclude(row["title"])

    def _on_codex_override(self, title, kind, value):
        values = self._codex_config[kind + "_overrides"]
        if value:
            values[title] = value
        else:
            values.pop(title, None)
        self._apply_codex_config()

    def _on_codex_loops_spent(self, title, left):
        self._codex_config["after_finish_loops"][title] = left
        self._save_cli_settings()
        self._last_render_sig = None
        self._render_table()

    def _on_codex_after_finish(self, title):
        dlg = QDialog(self)
        dlg.setWindowTitle("Codex CLI — After finish")
        layout = QVBoxLayout(dlg)
        hint = QLabel(f"Prompt for {title!r} after a completed turn sits idle. Empty = off. Use one line.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        edit = QPlainTextEdit(self._codex_config["after_finish"].get(title, ""))
        layout.addWidget(edit)
        loops = QSpinBox()
        loops.setRange(-1, 10000)
        loops.setSpecialValueText("Unlimited")
        initial_left = self._codex_config["after_finish_loops"].get(title, 1)
        loops.setValue(initial_left)
        form = QFormLayout()
        form.addRow("Runs left (0 = spent)", loops)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        def accept():
            prompt = edit.toPlainText().strip()
            if "\n" in prompt or "\r" in prompt:
                QMessageBox.warning(dlg, "Codex prompt", "Use a single-line prompt.")
                return
            old_prompt = self._codex_config["after_finish"].get(title, "")
            if prompt:
                self._codex_config["after_finish"][title] = prompt
                # Preserve any budget spent while this modal dialog was open.
                left = self._codex_config["after_finish_loops"].get(title, initial_left) if loops.value() == initial_left else loops.value()
                self._codex_config["after_finish_loops"][title] = 1 if left == 0 and prompt != old_prompt else left
            else:
                self._codex_config["after_finish"].pop(title, None)
                self._codex_config["after_finish_loops"].pop(title, None)
            self._apply_codex_config()
            dlg.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()
