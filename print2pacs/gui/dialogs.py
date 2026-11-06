"""Small modal dialogs."""

from __future__ import annotations

import customtkinter as ctk

from ..model import Node


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
