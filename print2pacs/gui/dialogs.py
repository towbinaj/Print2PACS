"""Small modal dialogs."""

from __future__ import annotations

from tkinter import messagebox

import customtkinter as ctk

from ..model import Node
from . import theme


class AddDestinationDialog(ctk.CTkToplevel):
    """Collect a PACS destination (name, AE, host, port, calling AE)."""

    def __init__(self, parent, on_save, initial: Node | None = None):
        super().__init__(parent)
        self.on_save = on_save
        self.title("Add PACS destination")
        self.geometry("420x360")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._vars = {}
        fields = [
            ("name", "Display name", initial.name if initial else ""),
            ("aetitle", "Remote AE Title", initial.aetitle if initial else ""),
            ("host", "Host / IP", initial.host if initial else ""),
            ("port", "Port", str(initial.port) if initial else "104"),
            ("calling_aetitle", "Calling AE (this app)",
             initial.calling_aetitle if initial else "PRINT2PACS"),
        ]
        for i, (key, label, default) in enumerate(fields):
            ctk.CTkLabel(self, text=label).grid(row=i, column=0, sticky="w",
                                                padx=16, pady=(12 if i == 0 else 6, 0))
            var = ctk.StringVar(value=default)
            self._vars[key] = var
            ctk.CTkEntry(self, textvariable=var, width=250).grid(
                row=i, column=1, sticky="e", padx=16,
                pady=(12 if i == 0 else 6, 0))

        self._tls = ctk.BooleanVar(value=initial.tls if initial else False)
        ctk.CTkCheckBox(self, text="Use TLS", variable=self._tls).grid(
            row=len(fields), column=1, sticky="e", padx=16, pady=(10, 0))

        self.err = ctk.CTkLabel(self, text="", text_color="#d9534f",
                                wraplength=380, justify="left")
        self.err.grid(row=len(fields) + 1, column=0, columnspan=2,
                      sticky="w", padx=16, pady=(8, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=len(fields) + 2, column=0, columnspan=2, pady=12)
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="gray",
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Save", width=90,
                      command=self._save).pack(side="left", padx=6)

    def _save(self):
        try:
            port = int(self._vars["port"].get())
        except ValueError:
            self.err.configure(text="Port must be a number.")
            return
        node = Node(
            name=self._vars["name"].get().strip(),
            aetitle=self._vars["aetitle"].get().strip(),
            host=self._vars["host"].get().strip(),
            port=port,
            calling_aetitle=self._vars["calling_aetitle"].get().strip(),
            tls=self._tls.get(),
        )
        problems = node.validate()
        if problems:
            self.err.configure(text="  ".join(problems))
            return
        self.on_save(node)
        self.destroy()


class RecoveryDialog(ctk.CTkToplevel):
    """Offer to resume / review / discard sends interrupted by a crash or close."""

    def __init__(self, parent, jobs, on_resume, on_discard):
        super().__init__(parent)
        self.on_resume = on_resume
        self.on_discard = on_discard
        self.title("Unfinished sends")
        self.geometry("580x400")
        self.minsize(520, 320)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self, text="These sends did not finish. Resume re-sends the same "
            "objects (no duplicates); Open shows the files to review; Discard "
            "deletes them.", wraplength=540, justify="left").pack(
            padx=16, pady=(14, 6), anchor="w")

        body = ctk.CTkScrollableFrame(self)
        body.pack(fill="both", expand=True, padx=12, pady=6)
        body.grid_columnconfigure(0, weight=1)

        from ..engine import spool
        for job_dir, journal in jobs:
            card = ctk.CTkFrame(body)
            card.pack(fill="x", pady=4, padx=2)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text=spool.summary(journal), anchor="w",
                         justify="left", wraplength=360).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=10, pady=8)
            ctk.CTkButton(
                card, text="Resume", width=84,
                command=lambda d=job_dir, j=journal: self._resume(d, j)).grid(
                row=0, column=1, padx=4, pady=(8, 2))
            ctk.CTkButton(
                card, text="Open", width=84, fg_color="gray",
                command=lambda d=job_dir: self._open(d)).grid(
                row=0, column=2, padx=(4, 10), pady=(8, 2))
            ctk.CTkButton(
                card, text="Discard", width=84, fg_color=theme.DANGER,
                command=lambda d=job_dir, j=journal, c=card:
                    self._discard(d, j, c)).grid(
                row=1, column=1, columnspan=2, padx=(4, 10), pady=(0, 8),
                sticky="e")

        ctk.CTkButton(self, text="Close", width=90,
                      command=self.destroy).pack(pady=10)

    def _resume(self, job_dir, journal):
        self.destroy()
        self.on_resume(job_dir, journal)

    def _open(self, job_dir):
        import os
        try:
            os.startfile(str(job_dir))      # noqa: S606 - the user's own folder
        except Exception:  # noqa: BLE001
            messagebox.showinfo("Folder", str(job_dir))

    def _discard(self, job_dir, journal, card):
        if messagebox.askyesno(
                "Discard job",
                "Permanently delete this unfinished job and its DICOM files? "
                "This cannot be undone."):
            self.on_discard(job_dir, journal)
            card.destroy()
