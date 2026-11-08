"""Standalone Print2PACS wizard: Images -> Find study -> Send.

Designed for non-IT users: three clear steps, drag-and-drop, and a single
"Send to PACS" action. Heavy work (C-FIND / C-STORE) runs off the UI thread.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from dataclasses import replace
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:  # noqa: BLE001
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

from .. import APP_NAME, __version__, config, paths
from ..engine import dicom_import, limits, reconcile, sc_import, spool, video
from ..logging_setup import get_logger, setup as setup_logging
from ..model import (BODY_PARTS_EXAMINED, CONVERSION_TYPES, SendIntent,
                     SeriesInfo, StudyContext)
from ..net import scu
from . import theme
from .dialogs import AddDestinationDialog, RecoveryDialog
from .widgets import run_threaded

log = get_logger("gui")

_APP_BASES = (ctk.CTk, TkinterDnD.DnDWrapper) if _HAS_DND else (ctk.CTk,)

# Modality dropdown: sentinel for per-file-type auto selection, then manual codes.
AUTO_MODALITY = "Auto (by type)"
MODALITY_VALUES = [AUTO_MODALITY, "OT", "SC", "XC", "DOC", "CR", "DX"]


class App(*_APP_BASES):
    def __init__(self):
        super().__init__()
        self._dnd_ok = False
        if _HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self._dnd_ok = True
            except Exception:  # noqa: BLE001
                self._dnd_ok = False

        setup_logging()
        self.settings = config.load_settings()
        self.destinations = config.load_destinations()

        ctk.set_appearance_mode(self.settings.appearance)
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME}  v{__version__}")
        self.minsize(760, 560)
        self.geometry("860x660")

        icon = paths.resource_dir() / "assets" / "icon.ico"
        if icon.exists():
            try:
                self.iconbitmap(str(icon))
            except Exception:  # noqa: BLE001
                pass

        # ---- shared wizard state ----
        self.pages: list[sc_import.SourcePage] = []
        # videos: list of (path, VideoMeta, pil_thumb_or_None)
        self.videos: list[tuple] = []
        # dicoms: existing DICOM objects to re-file onto the chosen study
        self.dicoms: list[dicom_import.DicomSource] = []
        self._thumb_imgs: list = []          # keep CTkImage refs alive
        self.studies: list[StudyContext] = []
        self.chosen_study: StudyContext | None = None
        self._step = 1
        self._sending = False
        # Monotonic token so an older, slower C-FIND callback can never replace
        # the results/selection from a newer lookup (stale-result suppression).
        self._lookup_gen = 0
        # Name of the PACS the current study was looked up on (for the
        # query->send namespace guard).
        self._lookup_query_name = ""
        # Fingerprint the re-file confirmation was bound to (invalidated on change).
        self._confirmed_fp = ""

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._update_info = None
        self._build_header()
        self._build_banner()
        self._build_content()
        self._build_nav()
        self._build_statusbar()
        self._show_step(1)
        self._enable_window_drop()

        # Recover any send that was interrupted by a crash / forced close.
        self.after(700, self._check_recovery)

        # Quiet background update check (built exe only).
        from .. import updater
        if updater.can_self_update():
            self.after(2500, lambda: self.check_updates(manual=False))

    # ------------------------------------------------------------------ header
    def _build_header(self):
        head = ctk.CTkFrame(self, corner_radius=0, height=64)
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.step_title = ctk.CTkLabel(
            head, text="", font=ctk.CTkFont(size=18, weight="bold"),
            anchor="w")
        self.step_title.grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        self.step_sub = ctk.CTkLabel(head, text="", text_color=theme.MUTED,
                                     anchor="w")
        self.step_sub.grid(row=1, column=0, sticky="w", padx=18, pady=(0, 12))
        self.step_dots = ctk.CTkLabel(
            head, text="", font=ctk.CTkFont(size=13, weight="bold"),
            text_color=theme.MUTED)
        self.step_dots.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 6))
        ctk.CTkButton(head, text="⚙ Settings", width=100, fg_color="gray",
                      command=self._open_settings).grid(
            row=0, column=2, rowspan=2, sticky="e", padx=(0, 14))

    # ------------------------------------------------- persistent patient banner
    def _build_banner(self):
        """A pinned banner showing the selected patient; visible from the moment
        a study is chosen through review/send, so the target identity is always
        in view. Display-only."""
        self.banner_frame = ctk.CTkFrame(self, corner_radius=0,
                                         fg_color=theme.ACCENT)
        self.banner_frame.grid(row=1, column=0, sticky="ew")
        self.banner_frame.grid_columnconfigure(0, weight=1)
        self.patient_banner = ctk.CTkLabel(
            self.banner_frame, text="", anchor="w", text_color="white",
            font=ctk.CTkFont(size=13, weight="bold"))
        self.patient_banner.grid(row=0, column=0, sticky="w", padx=14, pady=4)
        self.banner_frame.grid_remove()        # hidden until a study is selected

    def _update_banner(self):
        s = self.chosen_study
        if s and s.study_uid:
            name = s.patient_name.replace("^", ", ")
            issuer = (f" · issuer {s.issuer_of_patient_id}"
                      if s.issuer_of_patient_id else " · ⚠ no issuer")
            self.patient_banner.configure(text=(
                f"▶ Filing to:  {name}   MRN {s.patient_id}{issuer}   "
                f"DOB {s.birth_date or '—'}   Sex {s.sex or '—'}   "
                f"Acc {s.accession or '—'}   {s.study_description or ''}".rstrip()))
            self.banner_frame.grid()
        else:
            self.banner_frame.grid_remove()

    # ----------------------------------------------------------------- content
    def _build_content(self):
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.frames: dict[int, ctk.CTkFrame] = {}
        self.frames[1] = self._build_step1()
        self.frames[2] = self._build_step2()
        self.frames[3] = self._build_step3()
        for f in self.frames.values():
            f.grid(row=0, column=0, sticky="nsew")

    # --------- Step 1: images ---------
    def _build_step1(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_rowconfigure(1, weight=1)
        f.grid_columnconfigure(0, weight=1)

        self.drop = ctk.CTkFrame(f, height=90, fg_color=theme.DROP_IDLE,
                                 corner_radius=10)
        self.drop.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.drop.grid_propagate(False)
        msg = ("Drop files anywhere in this window  —  or  "
               if self._dnd_ok else "")
        inner = ctk.CTkFrame(self.drop, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(inner, text=msg, text_color=theme.MUTED).pack(side="left")
        ctk.CTkButton(inner, text="Add files…", width=120,
                      command=self._add_files).pack(side="left")
        ctk.CTkLabel(f, text="Images: JPG · PNG · TIFF · BMP · PDF      "
                             "Video: MP4 · MOV · AVI · MKV      DICOM: .dcm",
                     text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0,
                                                     sticky="s", pady=(0, 2))

        self.thumb_list = ctk.CTkScrollableFrame(f, label_text="Pages to send")
        self.thumb_list.grid(row=1, column=0, sticky="nsew")
        self.thumb_list.grid_columnconfigure(0, weight=1)

        self.count_lbl = ctk.CTkLabel(f, text="Pages to send: 0",
                                      font=ctk.CTkFont(weight="bold"))
        self.count_lbl.grid(row=2, column=0, sticky="w", pady=(8, 0))

        if self._dnd_ok:
            self._enable_drop(self.drop, self._on_drop)
            self._enable_drop(self.thumb_list, self._on_drop)
        return f

    # --------- Step 2: find study ---------
    def _build_step2(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(2, weight=1)

        dest = ctk.CTkFrame(f)
        dest.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        dest.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dest, text="Query PACS:").grid(row=0, column=0, padx=12,
                                                    pady=12)
        self.query_menu = ctk.CTkOptionMenu(dest, values=["(none)"],
                                            command=lambda _v: None)
        self.query_menu.grid(row=0, column=1, sticky="w", padx=6, pady=12)
        ctk.CTkButton(dest, text="＋ Add PACS…", width=110,
                      command=self._add_destination).grid(row=0, column=2,
                                                          padx=6)
        ctk.CTkButton(dest, text="Test", width=60, fg_color="gray",
                      command=self._test_connection).grid(row=0, column=3,
                                                          padx=(6, 12))

        search = ctk.CTkFrame(f)
        search.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        search.grid_columnconfigure((1, 3), weight=1)
        ctk.CTkLabel(search, text="Accession #:").grid(row=0, column=0,
                                                       padx=12, pady=12)
        self.acc_var = ctk.StringVar()
        acc = ctk.CTkEntry(search, textvariable=self.acc_var)
        acc.grid(row=0, column=1, sticky="ew", padx=6, pady=12)
        acc.bind("<Return>", lambda _e: self._lookup())
        ctk.CTkLabel(search, text="Patient ID:").grid(row=0, column=2,
                                                      padx=12, pady=12)
        self.pid_var = ctk.StringVar()
        pid = ctk.CTkEntry(search, textvariable=self.pid_var)
        pid.grid(row=0, column=3, sticky="ew", padx=6, pady=12)
        pid.bind("<Return>", lambda _e: self._lookup())
        self.lookup_btn = ctk.CTkButton(search, text="Look up", width=100,
                                        command=self._lookup)
        self.lookup_btn.grid(row=0, column=4, padx=(6, 12))

        self.results = ctk.CTkScrollableFrame(f, label_text="Matching study")
        self.results.grid(row=2, column=0, sticky="nsew")
        self.results.grid_columnconfigure(0, weight=1)

        self.find_status = ctk.CTkLabel(f, text="", text_color=theme.MUTED,
                                        anchor="w", wraplength=760,
                                        justify="left")
        self.find_status.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        return f

    # --------- Step 3: series + send ---------
    def _build_step3(self):
        f = ctk.CTkFrame(self.content, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        self.confirm_box = ctk.CTkFrame(f)
        self.confirm_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.confirm_box.grid_columnconfigure(1, weight=1)
        self.confirm_lbl = ctk.CTkLabel(self.confirm_box, text="", anchor="w",
                                        justify="left", wraplength=780)
        self.confirm_lbl.grid(row=0, column=0, columnspan=2, sticky="w",
                              padx=14, pady=(12, 6))
        ctk.CTkLabel(self.confirm_box, text="Send to PACS:").grid(
            row=1, column=0, sticky="w", padx=14, pady=(0, 12))
        self.send_menu = ctk.CTkOptionMenu(self.confirm_box, values=["(none)"],
                                           command=lambda _v: None)
        self.send_menu.grid(row=1, column=1, sticky="w", padx=6, pady=(0, 12))

        # Shown only when re-filing existing DICOM objects (see _refresh_confirm):
        # a deliberate guard against sending the wrong patient's material.
        self.dicom_confirm_var = ctk.BooleanVar(value=False)
        self.dicom_confirm_chk = ctk.CTkCheckBox(
            self.confirm_box, variable=self.dicom_confirm_var,
            command=self._on_dicom_confirm,
            text="I confirm this material belongs to this patient")

        series = ctk.CTkFrame(f)
        series.grid(row=1, column=0, sticky="new", pady=(0, 10))
        series.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(series, text="New series details",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 4))

        ctk.CTkLabel(series, text="Modality:").grid(row=1, column=0, sticky="w",
                                                    padx=12, pady=6)
        init_mod = (self.settings.default_modality
                    if self.settings.default_modality in MODALITY_VALUES
                    else AUTO_MODALITY)
        self.modality_var = ctk.StringVar(value=init_mod)
        ctk.CTkOptionMenu(series, variable=self.modality_var,
                          values=MODALITY_VALUES, width=160).grid(
            row=1, column=1, sticky="w", padx=6)
        ctk.CTkLabel(series,
                     text="Auto: PDF/TIFF→OT, photos→XC; dropped DICOM keeps its own.",
                     text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(
            row=1, column=2, sticky="w", padx=(6, 12))

        ctk.CTkLabel(series, text="Series description:").grid(
            row=2, column=0, sticky="w", padx=12, pady=6)
        self.desc_var = ctk.StringVar(
            value=self.settings.default_series_description)
        ctk.CTkEntry(series, textvariable=self.desc_var).grid(
            row=2, column=1, sticky="ew", padx=6, pady=6)

        ctk.CTkLabel(series, text="Body part:").grid(row=3, column=0,
                                                     sticky="w", padx=12, pady=6)
        self.body_var = ctk.StringVar()
        # Editable: pick a standard DICOM term or type a custom one / leave blank.
        ctk.CTkComboBox(series, variable=self.body_var,
                        values=[""] + BODY_PARTS_EXAMINED, width=240).grid(
            row=3, column=1, sticky="w", padx=6, pady=6)

        ctk.CTkLabel(series, text="Capture type:").grid(row=4, column=0,
                                                        sticky="w", padx=12,
                                                        pady=6)
        ct_values = [f"{k} - {v}" for k, v in CONVERSION_TYPES.items()]
        self.conv_var = ctk.StringVar(
            value=next(v for v in ct_values
                       if v.startswith(self.settings.default_conversion_type)))
        ctk.CTkOptionMenu(series, variable=self.conv_var, values=ct_values,
                          width=220).grid(row=4, column=1, sticky="w", padx=6,
                                          pady=(6, 12))

        prog = ctk.CTkFrame(f, fg_color="transparent")
        prog.grid(row=2, column=0, sticky="ew")
        prog.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(prog)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(4, 2))
        self.cancel_btn = ctk.CTkButton(prog, text="Cancel", width=90,
                                        fg_color=theme.DANGER,
                                        command=self._cancel_send)
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))
        self.cancel_btn.grid_remove()          # shown only during a send
        self.send_status = ctk.CTkLabel(prog, text="", text_color=theme.MUTED,
                                        anchor="w", wraplength=780,
                                        justify="left")
        self.send_status.grid(row=1, column=0, sticky="ew")
        return f

    # -------------------------------------------------------------------- nav
    def _build_nav(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=56)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.back_btn = ctk.CTkButton(bar, text="‹ Back", width=90,
                                      fg_color="gray", command=self._back)
        self.back_btn.grid(row=0, column=1, padx=6, pady=10)
        self.next_btn = ctk.CTkButton(bar, text="Next ›", width=140,
                                      fg_color=theme.ACCENT,
                                      hover_color=theme.ACCENT_HOVER,
                                      command=self._next)
        self.next_btn.grid(row=0, column=2, padx=(6, 14), pady=10)

    # ------------------------------------------------------------- status bar
    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, height=26)
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(bar, text=f"Data: {paths.data_dir()}",
                     text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0,
                                                     sticky="w", padx=8)
        self.update_btn = ctk.CTkButton(
            bar, text="", height=20, fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._start_update)
        # shown only when an update is found
        ctk.CTkLabel(bar, text=f"v{__version__}", text_color=theme.MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=2,
                                                     sticky="e", padx=8)

    # ---------------------------------------------------------------- updates
    def check_updates(self, manual: bool = False):
        from .. import updater
        if manual and not updater.can_self_update():
            messagebox.showinfo(
                APP_NAME, f"You're on v{__version__}.\n\nAutomatic self-update "
                "works in the built .exe. From source, update with git.")
            return

        def work():
            return updater.check_latest()

        def done(info):
            self._on_update_result(info, manual)

        def err(exc):
            if manual:
                messagebox.showinfo(APP_NAME, f"Update check failed:\n{exc}")

        run_threaded(self, work, done, err)

    def _on_update_result(self, info, manual: bool):
        from .. import updater
        self._update_info = info
        if info:
            self.update_btn.configure(text=f"⬆ Update to v{info.version}")
            self.update_btn.grid(row=0, column=1, sticky="e", padx=8, pady=2)
            if manual and messagebox.askyesno(
                    "Update available",
                    f"Version {info.version} is available (you have "
                    f"{updater.current_version()}).\n\nInstall now? The app will "
                    "restart; your settings and destinations are kept."):
                self._start_update()
        elif manual:
            messagebox.showinfo(
                APP_NAME, f"You're on the latest version (v{__version__}).")

    def _start_update(self):
        from .. import updater
        info = self._update_info
        if not info:
            return
        if not updater.can_self_update():
            messagebox.showinfo(
                APP_NAME, f"Version {info.version} is available. Update the "
                "built .exe to install it (self-update needs the built exe).")
            return
        if not messagebox.askyesno(
                "Install update",
                f"Download and install v{info.version}? The app will close and "
                "reopen on the new version. Your settings are preserved."):
            return

        top = ctk.CTkToplevel(self)
        top.title("Updating")
        top.geometry("360x110")
        top.transient(self)
        ctk.CTkLabel(top, text=f"Downloading v{info.version}…").pack(pady=(18, 6))
        pbar = ctk.CTkProgressBar(top, width=300)
        pbar.set(0)
        pbar.pack(pady=6)

        def prog(frac):
            try:
                self.after(0, lambda: pbar.set(frac))
            except Exception:  # noqa: BLE001
                pass

        def work():
            updater.apply_update(info, progress=prog)
            self.after(0, self.destroy)   # exit so the helper can swap the exe

        def err(exc):
            try:
                top.destroy()
            except Exception:  # noqa: BLE001
                pass
            messagebox.showerror("Update failed", str(exc))

        run_threaded(self, work, None, err)

    # =====================================================================
    # Navigation
    # =====================================================================
    _TITLES = {
        1: ("Step 1 of 3 — Select images",
            "Add the pages you want to send. Drag them in or browse."),
        2: ("Step 2 of 3 — Find the study",
            "Look up the existing study in PACS by accession number."),
        3: ("Step 3 of 3 — Review and send",
            "Confirm the patient, set the series details, and send."),
    }

    def _show_step(self, n: int):
        self._step = n
        self.frames[n].tkraise()
        title, sub = self._TITLES[n]
        self.step_title.configure(text=title)
        self.step_sub.configure(text=sub)
        self.step_dots.configure(
            text="  ".join(("●" if i == n else "○") for i in (1, 2, 3)))
        self.back_btn.configure(state="normal" if n > 1 else "disabled")
        if n == 2:
            self._refresh_dest_menus()
        if n == 3:
            self._refresh_dest_menus()
            self.next_btn.configure(text="Send to PACS ⟶")
            self._refresh_confirm()
        else:
            self.next_btn.configure(text="Next ›")

    def _back(self):
        if self._step > 1 and not self._sending:
            self._show_step(self._step - 1)

    def _next(self):
        if self._step == 1:
            if not self.pages and not self.videos and not self.dicoms:
                messagebox.showwarning(
                    APP_NAME, "Add at least one image, video or DICOM first.")
                return
            self._show_step(2)
        elif self._step == 2:
            if not self.chosen_study:
                messagebox.showwarning(
                    APP_NAME, "Look up and select a study first.")
                return
            self._show_step(3)
        elif self._step == 3:
            self._send()

    # =====================================================================
    # Step 1 — images
    # =====================================================================
    @staticmethod
    def _is_accepted(p):
        return (sc_import.is_supported(p) or video.is_video(p)
                or dicom_import.is_dicom(p))

    def _add_files(self):
        img = " ".join(f"*{e}" for e in sorted(sc_import.SUPPORTED_EXTS))
        vid = " ".join(f"*{e}" for e in sorted(video.VIDEO_EXTS))
        dcm = " ".join(f"*{e}" for e in sorted(dicom_import.DICOM_EXTS))
        files = filedialog.askopenfilenames(
            title="Select images, PDF, video, or DICOM",
            filetypes=[("Images / PDF", img), ("Video", vid),
                       ("DICOM", dcm), ("All files", "*.*")])
        if files:
            self._ingest(list(files))

    def _on_drop(self, paths_list):
        self._ingest(paths_list)

    def _ingest(self, file_paths):
        accepted = [p for p in file_paths if self._is_accepted(p)]
        skipped = len(file_paths) - len(accepted)
        if not accepted:
            messagebox.showwarning(
                APP_NAME, "No supported files. Accepted: images (JPG/PNG/TIFF/"
                "BMP/PDF), video (MP4/MOV/AVI/MKV) and DICOM objects (.dcm).")
            return
        # Classify by precedence: image/PDF -> video -> DICOM (no double counting).
        img_paths = [p for p in accepted if sc_import.is_supported(p)]
        vid_paths = [p for p in accepted
                     if not sc_import.is_supported(p) and video.is_video(p)]
        dcm_paths = [p for p in accepted
                     if not sc_import.is_supported(p) and not video.is_video(p)
                     and dicom_import.is_dicom(p)]

        # Preflight the page budget before any heavy rendering (PDF/TIFF counts
        # are read from metadata, not decoded).
        incoming_pages = sum(limits.count_pages(p) for p in img_paths)
        perr = limits.pages_error(len(self.pages) + incoming_pages,
                                  self.settings.max_pages_per_job)
        if perr:
            messagebox.showwarning(APP_NAME, perr)
            return

        def work():
            pages = sc_import.load_many(img_paths, self.settings.pdf_dpi)
            vids = []
            for vp in vid_paths:
                meta = video.probe(vp)            # runs ffmpeg
                thumb = video.first_frame(vp)
                vids.append((vp, meta, thumb))
            dcms = [dicom_import.load_dicom(dp) for dp in dcm_paths]
            return pages, vids, dcms

        def done(result):
            pages, vids, dcms = result
            kept_vids, rejected = [], []
            for item in vids:
                meta = item[1]
                verr = limits.video_duration_error(
                    getattr(meta, "duration", 0) or 0,
                    self.settings.max_video_seconds)
                if verr:
                    rejected.append(verr)
                else:
                    kept_vids.append(item)
            self.pages.extend(pages)
            self.videos.extend(kept_vids)
            self.dicoms.extend(dcms)
            self._refresh_thumbs()
            if skipped:
                self.count_lbl.configure(
                    text=f"{self._item_count()} item(s)  "
                         f"({skipped} unsupported skipped)")
            if rejected:
                messagebox.showwarning(APP_NAME, "\n\n".join(rejected))

        def err(exc):
            messagebox.showerror(
                APP_NAME, f"Could not load files:\n{exc}\n\n"
                "(Video needs ffmpeg — run scripts/fetch_ffmpeg.py or install it.)")
            self._refresh_thumbs()

        self.count_lbl.configure(text="Loading…")
        run_threaded(self, work, done, err)

    def _item_count(self):
        return len(self.pages) + len(self.videos) + len(self.dicoms)

    def _refresh_thumbs(self):
        for w in self.thumb_list.winfo_children():
            w.destroy()
        self._thumb_imgs = []
        for i, page in enumerate(self.pages):
            row = ctk.CTkFrame(self.thumb_list)
            row.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)

            t = page.image.copy()
            t.thumbnail((84, 84))
            img = ctk.CTkImage(light_image=t, dark_image=t, size=t.size)
            self._thumb_imgs.append(img)
            ctk.CTkLabel(row, image=img, text="").grid(row=0, column=0,
                                                       rowspan=2, padx=8, pady=6)
            label = page.source
            if page.page_index:
                label += f"  (page {page.page_index + 1})"
            ctk.CTkLabel(row, text=f"{i + 1}.  {label}", anchor="w").grid(
                row=0, column=1, rowspan=2, sticky="w")

            ctk.CTkButton(row, text="▲", width=30,
                          command=lambda k=i: self._move(k, -1)).grid(
                row=0, column=2, padx=2)
            ctk.CTkButton(row, text="▼", width=30,
                          command=lambda k=i: self._move(k, 1)).grid(
                row=0, column=3, padx=2)
            ctk.CTkButton(row, text="✕", width=30, fg_color=theme.DANGER,
                          command=lambda k=i: self._remove(k)).grid(
                row=0, column=4, padx=(2, 8))

        base = len(self.pages)
        for j, (vpath, meta, thumb) in enumerate(self.videos):
            row = ctk.CTkFrame(self.thumb_list)
            row.grid(row=base + j, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)
            if thumb is not None:
                t = thumb.copy()
                t.thumbnail((84, 84))
                img = ctk.CTkImage(light_image=t, dark_image=t, size=t.size)
                self._thumb_imgs.append(img)
                ctk.CTkLabel(row, image=img, text="").grid(
                    row=0, column=0, rowspan=2, padx=8, pady=6)
            else:
                ctk.CTkLabel(row, text="🎞", width=84,
                             font=ctk.CTkFont(size=28)).grid(
                    row=0, column=0, rowspan=2, padx=8, pady=6)
            from pathlib import Path as _P
            secs = f"{meta.duration:.0f}s" if meta.duration else "?"
            ctk.CTkLabel(
                row, anchor="w", justify="left",
                text=f"🎞 {_P(vpath).name}\n{meta.width}x{meta.height} · "
                     f"{meta.nframes or '?'} frames · {secs} → video").grid(
                row=0, column=1, rowspan=2, sticky="w")
            ctk.CTkButton(row, text="✕", width=30, fg_color=theme.DANGER,
                          command=lambda k=j: self._remove_video(k)).grid(
                row=0, column=4, padx=(2, 8))

        base2 = len(self.pages) + len(self.videos)
        for m, src in enumerate(self.dicoms):
            row = ctk.CTkFrame(self.thumb_list)
            row.grid(row=base2 + m, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text="🩻", width=84,
                         font=ctk.CTkFont(size=28)).grid(
                row=0, column=0, rowspan=2, padx=8, pady=6)
            from pathlib import Path as _P
            ctk.CTkLabel(
                row, anchor="w", justify="left",
                text=f"🩻 {_P(src.path).name}\n{src.label} → re-filed").grid(
                row=0, column=1, rowspan=2, sticky="w")
            ctk.CTkButton(row, text="✕", width=30, fg_color=theme.DANGER,
                          command=lambda k=m: self._remove_dicom(k)).grid(
                row=0, column=4, padx=(2, 8))

        n_img, n_vid, n_dcm = len(self.pages), len(self.videos), len(self.dicoms)
        parts = []
        if n_img:
            parts.append(f"{n_img} image page(s)")
        if n_vid:
            parts.append(f"{n_vid} video(s)")
        if n_dcm:
            parts.append(f"{n_dcm} DICOM object(s)")
        self.count_lbl.configure(
            text="To send: " + (", ".join(parts) if parts else "nothing yet"))

    def _move(self, idx, delta):
        j = idx + delta
        if 0 <= j < len(self.pages):
            self.pages[idx], self.pages[j] = self.pages[j], self.pages[idx]
            self._refresh_thumbs()

    def _remove(self, idx):
        if 0 <= idx < len(self.pages):
            del self.pages[idx]
            self._refresh_thumbs()

    def _remove_video(self, idx):
        if 0 <= idx < len(self.videos):
            del self.videos[idx]
            self._refresh_thumbs()

    def _remove_dicom(self, idx):
        if 0 <= idx < len(self.dicoms):
            del self.dicoms[idx]
            self._refresh_thumbs()

    # =====================================================================
    # Step 2 — find study
    # =====================================================================
    _NONE = "(no PACS configured)"

    def _refresh_dest_menus(self):
        names = [n.name for n in self.destinations]
        self._apply_menu(self.query_menu, names,
                         self.settings.query_destination)
        self._apply_menu(self.send_menu, names,
                         self.settings.send_destination
                         or self.settings.query_destination)

    def _apply_menu(self, menu, names, want):
        if names:
            menu.configure(values=names, state="normal")
            if want in names:
                menu.set(want)
            elif menu.get() not in names:
                menu.set(names[0])
        else:
            menu.configure(values=[self._NONE], state="disabled")
            menu.set(self._NONE)

    def _node_by_name(self, name):
        for n in self.destinations:
            if n.name == name:
                return n
        return None

    def _query_node(self):
        return self._node_by_name(self.query_menu.get())

    def _send_node(self):
        return self._node_by_name(self.send_menu.get())

    def _add_destination(self):
        def on_save(node):
            self.destinations.append(node)
            config.save_destinations(self.destinations)
            self._refresh_dest_menus()
            self.query_menu.set(node.name)
        AddDestinationDialog(self, on_save)

    def _open_settings(self):
        from .settings_view import SettingsDialog
        SettingsDialog(self, on_changed=self._refresh_dest_menus)

    def _test_connection(self):
        node = self._query_node()
        if not node:
            messagebox.showinfo(APP_NAME, "Add a PACS destination first.")
            return
        self.find_status.configure(text="Testing connection…")

        def work():
            return scu.c_echo(self.settings.my_aetitle, node,
                              tls_args=scu.tls_args_for(node))

        def done(res):
            self.find_status.configure(text=res.message)
            if not res.success:
                messagebox.showwarning(APP_NAME, res.message)

        run_threaded(self, work, done,
                     lambda e: self.find_status.configure(text=str(e)))

    def _lookup(self):
        node = self._query_node()
        if not node:
            messagebox.showinfo(APP_NAME, "Add a PACS destination first.")
            return
        acc = self.acc_var.get().strip()
        pid = self.pid_var.get().strip()
        if not acc and not pid:
            messagebox.showwarning(
                APP_NAME, "Enter an accession number or a patient ID.")
            return
        self.settings.query_destination = node.name
        config.save_settings(self.settings)
        self._lookup_query_name = node.name
        self.find_status.configure(text="Querying PACS…")
        # A new lookup invalidates any prior selection and any in-flight query.
        self.chosen_study = None
        self._update_banner()
        self._clear_results()
        self._lookup_gen += 1
        gen = self._lookup_gen
        self.lookup_btn.configure(state="disabled")

        def work():
            return reconcile.lookup_studies(
                self.settings.my_aetitle, node, accession=acc, patient_id=pid,
                tls_args=scu.tls_args_for(node))

        def done(studies):
            if gen != self._lookup_gen:
                return                       # a newer lookup superseded this one
            self.lookup_btn.configure(state="normal")
            self.studies = studies
            self._render_results()

        def err(exc):
            if gen != self._lookup_gen:
                return
            self.lookup_btn.configure(state="normal")
            self.find_status.configure(text=str(exc))
            self._clear_results()

        run_threaded(self, work, done, err)

    def _clear_results(self):
        for w in self.results.winfo_children():
            w.destroy()

    def _render_results(self):
        self._clear_results()
        if not self.studies:
            self.find_status.configure(
                text="No matching study found. Check the accession number.")
            return
        self.find_status.configure(
            text=f"{len(self.studies)} match(es). Select the correct study "
                 "to continue.")
        # No pre-selection: the operator must deliberately choose a study. This
        # prevents silently defaulting to the first of several matches.
        self._study_var = ctk.IntVar(value=-1)
        for i, s in enumerate(self.studies):
            card = ctk.CTkFrame(self.results)
            card.grid(row=i, column=0, sticky="ew", pady=4, padx=2)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkRadioButton(
                card, text="", variable=self._study_var, value=i,
                command=self._pick_study, width=24).grid(
                row=0, column=0, rowspan=2, padx=(10, 6), pady=10)
            name = s.patient_name.replace("^", ", ")
            ctk.CTkLabel(card, text=f"{name}   (ID {s.patient_id})",
                         font=ctk.CTkFont(weight="bold"), anchor="w").grid(
                row=0, column=1, sticky="w", pady=(8, 0))
            detail = (f"Acc {s.accession} · {s.study_description or '—'} · "
                      f"{s.study_date or '—'} · DOB {s.birth_date or '—'} · "
                      f"Sex {s.sex or '—'} · {s.modalities or '—'}")
            ctk.CTkLabel(card, text=detail, text_color=theme.MUTED,
                         anchor="w").grid(row=1, column=1, sticky="w",
                                          pady=(0, 8))
        # chosen_study stays None until the operator clicks a radio button.

    def _pick_study(self):
        idx = self._study_var.get()
        if 0 <= idx < len(self.studies):
            self.chosen_study = self.studies[idx]
            self._update_banner()
            self.find_status.configure(
                text="Study selected. Click Next to review and send.")

    # =====================================================================
    # Step 3 — review + send
    # =====================================================================
    def _refresh_confirm(self):
        s = self.chosen_study
        if not s:
            self.confirm_lbl.configure(text="(no study selected)")
            return
        name = s.patient_name.replace("^", ", ")
        bits = []
        if self.pages:
            bits.append(f"{len(self.pages)} image page(s)")
        if self.videos:
            bits.append(f"{len(self.videos)} video(s)")
        if self.dicoms:
            bits.append(f"{len(self.dicoms)} DICOM object(s)")
        note = ("\nDropped DICOM objects keep their original modality/type; "
                "patient & study tags are re-stamped to this study."
                if self.dicoms else "")
        issuer_txt = (f" · issuer {s.issuer_of_patient_id}"
                      if s.issuer_of_patient_id else " · ⚠ no MRN issuer returned")
        self.confirm_lbl.configure(text=(
            f"Patient:  {name}   (ID {s.patient_id}{issuer_txt})\n"
            f"Study:  Acc {s.accession} · {s.study_description or '—'} · "
            f"{s.study_date or '—'}\n"
            f"To send:  {', '.join(bits) or 'nothing'}{note}"))

        # Re-filed DICOM carries its own pixels/overlays (which header re-stamping
        # cannot change), so require an explicit per-send confirmation.
        if self.dicoms:
            self.dicom_confirm_var.set(False)
            self.dicom_confirm_chk.configure(
                text=f"I confirm these DICOM objects belong to {name}")
            self.dicom_confirm_chk.grid(row=2, column=0, columnspan=2,
                                        sticky="w", padx=14, pady=(0, 12))
        else:
            self.dicom_confirm_chk.grid_remove()

    def _series_info(self) -> SeriesInfo:
        conv = self.conv_var.get().split(" - ")[0].strip()
        sel = self.modality_var.get().strip()
        # In Auto mode the per-series modality is chosen at send time; carry a
        # safe placeholder here (still used for validation / non-image series).
        modality = "OT" if sel == AUTO_MODALITY else (sel or "OT")
        return SeriesInfo(
            modality=modality,
            series_description=self.desc_var.get().strip(),
            body_part=self.body_var.get().strip().upper(),
            series_number=self.settings.default_series_number,
            conversion_type=conv or "WSD",
        )

    # ---- send intent (identity integrity) -----------------------------------
    def _content_signature(self) -> str:
        """A hash of exactly which items are staged, so the intent fingerprint
        changes if items are added/removed/reordered or swapped."""
        parts: list[str] = []
        for p in self.pages:
            parts.append(f"img:{p.source}#{p.page_index}")
        for vpath, _m, _t in self.videos:
            try:
                st = os.stat(vpath)
                parts.append(f"vid:{vpath}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                parts.append(f"vid:{vpath}")
        for src in self.dicoms:
            parts.append(f"dcm:{src.content_sha256}")
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _build_intent(self, node) -> SendIntent:
        import getpass
        from datetime import datetime
        try:
            operator = getpass.getuser()
        except Exception:  # noqa: BLE001
            operator = ""
        return SendIntent.build(
            self.chosen_study or StudyContext(),
            query_source=self._lookup_query_name,
            destination=node.name if node else "",
            content_signature=self._content_signature(),
            operator=operator,
            confirmed_at=datetime.now().isoformat(timespec="seconds"))

    def _on_dicom_confirm(self):
        # Bind the confirmation to the current intent; a later change to the
        # study, destination, or items invalidates it (re-checked at send).
        self._confirmed_fp = (
            self._build_intent(self._send_node()).identity_fingerprint
            if self.dicom_confirm_var.get() else "")

    def _send(self):
        if self._sending:
            return
        node = self._send_node()
        if not node or not self.chosen_study or (not self.pages
                                                 and not self.videos
                                                 and not self.dicoms):
            messagebox.showwarning(APP_NAME,
                                   "Missing study, PACS, or items to send.")
            return
        si = self._series_info()
        if not si.series_description:
            messagebox.showwarning(APP_NAME, "Enter a series description.")
            return

        # Build the immutable send intent and fail closed on bad target identity.
        intent = self._build_intent(node)
        problems = intent.validate()
        if problems:
            messagebox.showwarning(
                APP_NAME, "Can't send — the target identity is incomplete:\n\n"
                + "\n".join(f"• {p}" for p in problems))
            return

        if self.dicoms and not self.dicom_confirm_var.get():
            messagebox.showwarning(
                APP_NAME, "Please confirm the dropped DICOM objects belong to "
                "this patient before sending.")
            return
        if self.dicoms and self._confirmed_fp != intent.identity_fingerprint:
            # Study, destination, or items changed after the confirmation.
            self.dicom_confirm_var.set(False)
            self._confirmed_fp = ""
            messagebox.showwarning(
                APP_NAME, "The study, destination, or items changed since you "
                "confirmed. Please re-confirm before sending.")
            return
        if self.dicoms:
            log.info("Operator confirmed %d re-filed DICOM object(s) belong to "
                     "the selected study.", len(self.dicoms))

        # Query->send namespace guard: the found StudyInstanceUID only means the
        # right thing if the send target shares the query node's namespace.
        qn = self._lookup_query_name
        if qn and node.name != qn:
            allowed = config.pair_allowed(qn, node.name)
            if allowed is False:                       # policy forbids this pair
                messagebox.showwarning(
                    APP_NAME, f"Policy does not permit sending a study queried "
                    f"from '{qn}' to '{node.name}'. Choose an approved "
                    "destination.")
                return
            if allowed is None and not messagebox.askyesno(
                    APP_NAME,
                    f"This study was found on '{qn}', but you are sending to "
                    f"'{node.name}'.\n\nOnly continue if both systems share the "
                    "same patient/study namespace (same enterprise). "
                    "Send anyway?"):
                return
            # allowed is True -> an approved pair; proceed

        perr = limits.pages_error(len(self.pages),
                                  self.settings.max_pages_per_job)
        if perr:
            messagebox.showwarning(APP_NAME, perr)
            return

        # persist defaults for next time (keep the raw selection, incl. Auto)
        self.settings.send_destination = node.name
        self.settings.default_modality = self.modality_var.get()
        self.settings.default_series_description = si.series_description
        self.settings.default_conversion_type = si.conversion_type
        config.save_settings(self.settings)

        self._begin_send_ui()
        self.send_status.configure(text="Converting images…")

        study = self.chosen_study
        pages = list(self.pages)
        videos = list(self.videos)
        dicoms = list(self.dicoms)
        my_ae = self.settings.my_aetitle
        keep = self.settings.keep_sent_copy
        comp = self.settings.image_compression
        strip_private = self.settings.strip_private_tags
        auto_mod = self.modality_var.get() == AUTO_MODALITY
        forced_mod = si.modality      # resolved code when not in Auto mode
        tls_args = scu.tls_args_for(node)
        prog, frac = self._mk_prog()

        def work():
            datasets = []
            snum = si.series_number
            if pages:
                if auto_mod:
                    # Split by per-file-type modality: one new series per code.
                    groups = sc_import.group_pages_by_modality(pages)
                    for mod, grp in groups.items():
                        gi = replace(si, modality=mod, series_number=snum)
                        snum += 1
                        datasets.extend(sc_import.convert_pages(
                            grp, study, gi, compression=comp))
                else:
                    fi = replace(si, series_number=snum)
                    snum += 1
                    datasets.extend(sc_import.convert_pages(
                        pages, study, fi, compression=comp))
            for vpath, _meta, _thumb in videos:
                prog(f"Transcoding video {os.path.basename(str(vpath))}…")
                # each video becomes its own new series (XC by default)
                vmod = "XC" if auto_mod else forced_mod
                vi = replace(si, modality=vmod, series_number=snum)
                snum += 1
                vds = video.convert_video(vpath, study, vi)
                verr = limits.video_size_error(
                    len(getattr(vds, "PixelData", b"") or b""),
                    self.settings.max_video_mb)
                if verr:
                    raise RuntimeError(verr)
                datasets.append(vds)
            if dicoms:
                prog(f"Re-filing {len(dicoms)} DICOM object(s)…")
                # re-filed objects are grouped into one new series per modality
                # (convert_dicoms assigns series numbers from di.series_number)
                di = replace(si, series_number=snum)
                datasets.extend(dicom_import.convert_dicoms(
                    dicoms, study, di, strip_private=strip_private))
            job_dir = spool.prepare_job(datasets, node, study)
            return spool.send_job(job_dir, my_ae, node, tls_args=tls_args,
                                  progress=prog, on_frac=frac, keep_copy=keep,
                                  should_cancel=self._cancel_event.is_set)

        def done(outcome):
            self._end_send_ui()
            self._after_send(outcome, keep, node)

        def err(exc):
            self._end_send_ui()
            messagebox.showerror(APP_NAME, f"Send failed:\n{exc}")

        run_threaded(self, work, done, err)

    # ---- send UI helpers + partial-failure recovery --------------------------
    def _begin_send_ui(self):
        self._sending = True
        self._cancel_event = threading.Event()
        self.next_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal", text="Cancel")
        self.cancel_btn.grid()
        self.progress.set(0)

    def _end_send_ui(self):
        self._sending = False
        self.next_btn.configure(state="normal")
        self.back_btn.configure(
            state="normal" if self._step > 1 else "disabled")
        self.cancel_btn.grid_remove()

    def _cancel_send(self):
        ev = getattr(self, "_cancel_event", None)
        if ev is not None:
            ev.set()
            self.cancel_btn.configure(state="disabled", text="Cancelling…")
            self.send_status.configure(
                text="Cancelling after the current object…")

    def _cooldown(self, seconds: int, reason: str):
        """Briefly block re-sending after a destination refusal / failure."""
        self.next_btn.configure(state="disabled")
        self.send_status.configure(text=reason)

        def _reenable():
            if not self._sending:
                self.next_btn.configure(state="normal")
        self.after(int(seconds * 1000), _reenable)

    def _mk_prog(self):
        def prog(msg):
            try:
                self.after(0, lambda: self.send_status.configure(text=msg))
            except Exception:  # noqa: BLE001
                pass

        def frac(v):
            try:
                self.after(0, lambda: self.progress.set(v))
            except Exception:  # noqa: BLE001
                pass
        return prog, frac

    def _after_send(self, outcome, keep, node):
        """Report a send outcome and, for recoverable failures, offer a retry
        that re-sends the SAME objects (same UIDs) without creating duplicates."""
        store = outcome.store
        study = self.chosen_study
        acc = study.accession if study else ""
        kept = (f"\n\nA copy is in:\n{outcome.job_dir}"
                if keep and not outcome.discarded else "")

        if getattr(outcome, "cancelled", False):
            c = outcome.counts
            messagebox.showwarning(
                APP_NAME, "Send cancelled.\n\nStored "
                f"{c['sent'] + c['warning']}, {outcome.resumable} not sent "
                "(kept — resume from the recovery prompt next launch, or send "
                f"again).{kept}")
            return

        if outcome.resolved and store.warnings == 0:
            self.progress.set(1)
            messagebox.showinfo(
                APP_NAME, f"Sent {store.sent} object(s) to PACS successfully.\n\n"
                f"New series added to accession {acc}.{kept}")
            self._reset_after_send()
            return

        if outcome.resolved:            # all stored, but some with warnings
            self.progress.set(1)
            detail = "\n".join(store.warning_details[:8])
            messagebox.showwarning(
                APP_NAME, f"PACS accepted {store.sent} object(s), but "
                f"{store.warnings} with warnings — the archive may have changed "
                "or dropped some data. Verify the new series on PACS.\n\n"
                f"{detail}{kept}")
            return

        # Not fully stored: there are unsent and/or uncertain objects.
        c = outcome.counts
        stored = c["sent"] + c["warning"]
        lines = list(store.errors[:5]) + list(store.uncertain_details[:5])
        detail = "\n".join(lines) or "Some objects were not confirmed stored."
        msg = (f"Stored {stored}, unsent {outcome.resumable}, uncertain "
               f"{outcome.uncertain}.\n\n{detail}")
        if outcome.uncertain:
            msg += ("\n\nUncertain object(s) were NOT re-sent — the connection "
                    "dropped before PACS confirmed them. Verify on PACS whether "
                    "they arrived before resending.")
        refused = any(("a700" in e.lower() or "association" in e.lower())
                      for e in store.errors)
        if refused and outcome.resumable:
            messagebox.showwarning(
                APP_NAME, msg + "\n\nThe destination refused new objects or was "
                "unreachable; waiting a few seconds before you can retry." + kept)
            self._cooldown(5, "Destination busy/unreachable — retry shortly.")
            return
        if outcome.resumable:
            if messagebox.askyesno(
                    APP_NAME, msg + f"\n\nRe-send the {outcome.resumable} unsent "
                    "object(s) now (same objects, no duplicates)?"):
                self._retry_job(outcome.job_dir, node, keep)
                return
        messagebox.showwarning(APP_NAME, msg + kept)

    def _retry_job(self, job_dir, node, keep):
        if self._sending or not node:
            return
        my_ae = self.settings.my_aetitle
        tls_args = scu.tls_args_for(node)
        self._begin_send_ui()
        self.send_status.configure(text="Re-sending unsent object(s)…")
        prog, frac = self._mk_prog()

        def work():
            return spool.send_job(job_dir, my_ae, node, tls_args=tls_args,
                                  progress=prog, on_frac=frac, keep_copy=keep,
                                  should_cancel=self._cancel_event.is_set)

        def done(outcome):
            self._end_send_ui()
            self._after_send(outcome, keep, node)

        def err(exc):
            self._end_send_ui()
            messagebox.showerror(APP_NAME, f"Re-send failed:\n{exc}")

        run_threaded(self, work, done, err)

    # ---- startup recovery of interrupted sends -------------------------------
    def _check_recovery(self):
        try:
            jobs = spool.unresolved_jobs()
        except Exception:  # noqa: BLE001
            return
        if jobs:
            RecoveryDialog(self, jobs, on_resume=self._resume_recovered,
                           on_discard=lambda d, j: spool.discard_job(d))

    def _resume_recovered(self, job_dir, journal):
        self._retry_job(job_dir, spool.node_from_journal(journal),
                        self.settings.keep_sent_copy)

    def _reset_after_send(self):
        self.pages = []
        self.videos = []
        self.dicoms = []
        self._refresh_thumbs()
        self.studies = []
        self.chosen_study = None
        self._update_banner()
        self._clear_results()
        self.acc_var.set("")
        self.pid_var.set("")
        self.find_status.configure(text="")
        self.progress.set(0)
        self.send_status.configure(text="")
        self._show_step(1)

    # ------------------------------------------------------------------- misc
    def _enable_window_drop(self):
        """Make the entire window a drop zone (not just the Step 1 banner)."""
        if self._dnd_ok:
            self._enable_drop(self, self._on_drop)

    def _enable_drop(self, widget, on_paths):
        try:
            widget.drop_target_register(DND_FILES)

            def _drop(e):
                on_paths(list(self.tk.splitlist(e.data)))
            widget.dnd_bind("<<Drop>>", _drop)
        except Exception:  # noqa: BLE001
            pass


# --- single-instance guard (prevents two windows -> accidental double sends) ---
_mutex_handle = None
_MUTEX_NAME = "Print2PACS.SingleInstance"


def _acquire_single_instance() -> bool:
    """True if we are the only instance; False if one is already running."""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        _mutex_handle = k.CreateMutexW(None, False, _MUTEX_NAME)
        return k.GetLastError() != 183          # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - never block launch on the guard itself
        return True


def _warn_already_running() -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "Print2PACS is already running. Use the open window.",
            "Print2PACS", 0x40)
    except Exception:  # noqa: BLE001
        pass


def main():
    if not _acquire_single_instance():
        _warn_already_running()
        return
    App().mainloop()


if __name__ == "__main__":
    main()
