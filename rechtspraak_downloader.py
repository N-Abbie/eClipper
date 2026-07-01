"""
rechtspraak_downloader.py
=========================
Bedieningsprogramma voor de export van uitspraken uit een uitsprakenoverzicht.

Drie manieren om te starten:

  GRAFISCH (standaard, dubbelklik / zonder argumenten):
      python rechtspraak_downloader.py

  SLEPEN: sleep een of meer bronbestanden (PDF, .eml, .msg, .html, .txt) op
  het programma (of op het .exe-icoon). De grafische modus opent dan met die
  bestanden al ingeladen en toont meteen hoeveel ECLI's zijn gevonden.

  OPDRACHTREGEL (bv. op een server zonder beeldscherm):
      python rechtspraak_downloader.py --run overzicht.pdf
      python rechtspraak_downloader.py --run a.pdf b.eml --out "D:/Uitspraken" --include-unlinked

Wat er wordt geexporteerd:
  - Standaard: elke uitspraak waar een hyperlink naar wijst (rechtspraak.nl).
  - Met --include-unlinked / het vinkje: ook ECLI's die alleen als tekst
    (zonder link) in de bron staan.
Elke uitspraak krijgt een voorblad met de basisgegevens en - als de bron een
L&S-uitsprakenoverzicht is - de samenvatting + "Samenvatting door ...".

De instellingen (opslagmap e.d.) worden bewaard.
"""

from __future__ import annotations

import os
import re
import sys
import queue
import threading
import argparse
import subprocess
from pathlib import Path

import rechtspraak_core as rc
import rechtspraak_sources as rs


# ===========================================================================
#  Hulpfuncties
# ===========================================================================

def _open_folder(path: str):
    p = Path(path)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)])
        else:
            subprocess.run(["xdg-open", str(p)])
    except Exception:
        pass


def _parse_drop(data: str) -> list[str]:
    """Splitst de data van een sleep-actie (tkinterdnd2) in losse paden."""
    out = []
    for m in re.finditer(r"\{([^}]*)\}|(\S+)", data or ""):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return [p for p in out if p]


def _summarize(results) -> dict:
    return {
        "ok": sum(1 for r in results if r.status == "ok"),
        "meta": sum(1 for r in results if r.status == "metadata-only"),
        "skip": sum(1 for r in results if r.status == "skipped"),
        "fail": sum(1 for r in results if r.status in ("not-found", "error")),
        "sum": sum(1 for r in results if r.status in ("ok", "metadata-only")
                   and r.has_summary),
        "unlinked": sum(1 for r in results if r.status in ("ok", "metadata-only")
                        and not r.linked),
        "fallback": sum(1 for r in results if r.status in ("ok", "metadata-only")
                        and r.title_source != "overzicht"),
    }


# ===========================================================================
#  Opdrachtregel-modus
# ===========================================================================

def run_cli(files, args) -> int:
    cfg = rc.Config.load()
    if args.out:
        cfg.output_dir = args.out
    if args.slash:
        cfg.slash_replacement = args.slash
    if args.include_unlinked:
        cfg.include_unlinked = True
    if args.no_report:
        cfg.generate_report = False
    if args.overwrite:
        cfg.overwrite_existing = True
    if args.underscores:
        cfg.spaces_to_underscores = True
    if args.delay is not None:
        cfg.request_delay = args.delay
    if args.save_config:
        cfg.save()
        print(f"Instellingen opgeslagen in: {rc.default_config_path()}")

    files = [f for f in files if f]
    if not files:
        print("Geen bronbestand opgegeven.", file=sys.stderr)
        return 2
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        for f in missing:
            print(f"Bestand niet gevonden: {f}", file=sys.stderr)
        return 2

    print("Bronnen   :", ", ".join(files))
    print(f"Opslagmap : {cfg.output_dir}")
    print("-" * 60)

    harvest = rs.harvest_files(files)
    for name, msg in harvest.errors:
        print(f"  Let op: {name}: {msg}", file=sys.stderr)
    print(f"Gedetecteerd: {harvest.n_linked} met link, "
          f"{harvest.n_unlinked} zonder link, "
          f"{harvest.summaries_found} met samenvatting.")
    if not cfg.include_unlinked and harvest.n_unlinked:
        print(f"  ({harvest.n_unlinked} zonder link worden NIET geexporteerd; "
              f"gebruik --include-unlinked om ze wel mee te nemen.)")
    print("-" * 60)

    results = rc.process_entries(harvest.entries, cfg, log_cb=print)
    report = ""
    if cfg.generate_report:
        report = rc.write_report(results, cfg.output_dir)

    s = _summarize(results)
    print("-" * 60)
    print(f"Klaar. Volledige tekst: {s['ok']} | alleen metadata: {s['meta']} | "
          f"met samenvatting: {s['sum']} | niet gelukt: {s['fail']}")
    if report:
        print(f"Rapport: {report}")
    return 0


# ===========================================================================
#  Grafische modus (Tkinter)
# ===========================================================================

def run_gui(initial_files=None) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # tkinterdnd2 is optioneel; nodig voor slepen IN het venster.
    dnd = False
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        root = tk.Tk()

    cfg = rc.Config.load()
    work = queue.Queue()
    files: list[str] = []
    counts = {"linked": 0, "unlinked": 0, "sum": 0}

    root.title("Uitspraken-export  (uitsprakenoverzicht \u2192 PDF's)")
    root.minsize(760, 620)

    pad = {"padx": 10, "pady": 4}
    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)
    main.columnconfigure(0, weight=1)

    out_var = tk.StringVar(value=cfg.output_dir)
    slash_var = tk.StringVar(value=cfg.slash_replacement)
    delay_var = tk.StringVar(value=str(cfg.request_delay))
    unlinked_var = tk.BooleanVar(value=cfg.include_unlinked)
    report_var = tk.BooleanVar(value=cfg.generate_report)
    over_var = tk.BooleanVar(value=cfg.overwrite_existing)
    under_var = tk.BooleanVar(value=cfg.spaces_to_underscores)

    # ---------- 1. Bronnen ----------
    hint = ("Sleep hier bronbestanden naartoe"
            if dnd else "Voeg bronbestanden toe")
    ttk.Label(main, text="1. Bronnen (PDF, e-mail .eml/.msg, .html, .txt)",
              font=("", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 2))

    srcframe = ttk.Frame(main)
    srcframe.grid(row=1, column=0, sticky="ew")
    srcframe.columnconfigure(0, weight=1)
    listbox = tk.Listbox(srcframe, height=4, activestyle="none")
    listbox.grid(row=0, column=0, sticky="ew")
    lsb = ttk.Scrollbar(srcframe, command=listbox.yview)
    lsb.grid(row=0, column=1, sticky="ns")
    listbox.config(yscrollcommand=lsb.set)

    srcbtns = ttk.Frame(main)
    srcbtns.grid(row=2, column=0, sticky="ew", pady=(4, 0))

    detect_var = tk.StringVar(value=hint + ".")
    detect_lbl = ttk.Label(main, textvariable=detect_var, foreground="#1f5fbf")
    detect_lbl.grid(row=3, column=0, sticky="w", pady=(4, 0))

    export_var = tk.StringVar(value="")
    ttk.Label(main, textvariable=export_var).grid(row=4, column=0, sticky="w")

    def refresh_listbox():
        listbox.delete(0, "end")
        for f in files:
            listbox.insert("end", "   " + Path(f).name)

    def update_export_label():
        if not files:
            export_var.set("")
            return
        n = counts["linked"] + (counts["unlinked"] if unlinked_var.get() else 0)
        export_var.set(f"Wordt geexporteerd: {n} uitspraak/uitspraken.")

    def run_detection():
        if not files:
            counts.update(linked=0, unlinked=0, sum=0)
            detect_var.set(hint + ".")
            update_export_label()
            return
        detect_var.set("Bezig met detecteren\u2026")
        snapshot = list(files)

        def job():
            try:
                h = rs.harvest_files(snapshot)
                work.put(("detect", h))
            except Exception as e:
                work.put(("detect_err", str(e)))
        threading.Thread(target=job, daemon=True).start()

    def add_files(paths):
        added = False
        for p in paths:
            p = os.path.abspath(p)
            if (os.path.isfile(p) and Path(p).suffix.lower() in rs.SUPPORTED_EXT
                    and p not in files):
                files.append(p)
                added = True
        if added:
            refresh_listbox()
            run_detection()

    def pick_files():
        ps = filedialog.askopenfilenames(
            title="Kies bronbestand(en)",
            filetypes=[
                ("Ondersteund", "*.pdf *.eml *.msg *.html *.htm *.txt *.md"),
                ("PDF", "*.pdf"), ("E-mail", "*.eml *.msg"),
                ("HTML", "*.html *.htm"), ("Tekst", "*.txt *.md"),
                ("Alle bestanden", "*.*")])
        if ps:
            add_files(list(ps))

    def clear_files():
        files.clear()
        refresh_listbox()
        run_detection()

    ttk.Button(srcbtns, text="Bestanden toevoegen\u2026",
               command=pick_files).grid(row=0, column=0, sticky="w")
    ttk.Button(srcbtns, text="Lijst wissen",
               command=clear_files).grid(row=0, column=1, sticky="w", padx=8)

    if dnd:
        def on_drop(event):
            add_files(_parse_drop(event.data))
        for w in (listbox, main):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", on_drop)
            except Exception:
                pass

    # ---------- 2. Instellingen ----------
    ttk.Separator(main).grid(row=5, column=0, sticky="ew", pady=8)
    ttk.Label(main, text="2. Instellingen",
              font=("", 11, "bold")).grid(row=6, column=0, sticky="w", pady=(0, 2))

    outframe = ttk.Frame(main)
    outframe.grid(row=7, column=0, sticky="ew")
    outframe.columnconfigure(1, weight=1)
    ttk.Label(outframe, text="Opslagmap:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(outframe, textvariable=out_var).grid(row=0, column=1, sticky="ew", **pad)

    def pick_out():
        d = filedialog.askdirectory(
            title="Kies de map om PDF's in op te slaan",
            initialdir=out_var.get() or os.path.expanduser("~"))
        if d:
            out_var.set(d)
    ttk.Button(outframe, text="Bladeren\u2026", command=pick_out).grid(
        row=0, column=2, **pad)

    opts = ttk.Frame(main)
    opts.grid(row=8, column=0, sticky="w", padx=10)
    ttk.Label(opts, text='"/" in partijnamen vervangen door:').grid(
        row=0, column=0, sticky="w", pady=3)
    ttk.Entry(opts, textvariable=slash_var, width=4).grid(
        row=0, column=1, sticky="w", padx=(6, 18))
    ttk.Label(opts, text="Pauze tussen verzoeken (sec):").grid(
        row=0, column=2, sticky="w", pady=3)
    ttk.Entry(opts, textvariable=delay_var, width=5).grid(
        row=0, column=3, sticky="w", padx=6)

    checks = ttk.Frame(main)
    checks.grid(row=9, column=0, sticky="w", padx=10, pady=(2, 4))
    ttk.Checkbutton(
        checks, text="Ook ECLI's zonder hyperlink meenemen (anders alleen gelinkte)",
        variable=unlinked_var,
        command=update_export_label).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(
        checks, text="Verwerkingsrapport genereren",
        variable=report_var).grid(row=1, column=0, sticky="w")
    ttk.Checkbutton(
        checks, text="Bestaande bestanden overschrijven",
        variable=over_var).grid(row=2, column=0, sticky="w")
    ttk.Checkbutton(
        checks, text="Spaties in bestandsnaam vervangen door underscores",
        variable=under_var).grid(row=3, column=0, sticky="w")

    # ---------- 3. Actie ----------
    ttk.Separator(main).grid(row=10, column=0, sticky="ew", pady=8)
    btns = ttk.Frame(main)
    btns.grid(row=11, column=0, sticky="ew")
    btns.columnconfigure(0, weight=1)
    start_btn = ttk.Button(btns, text="Start export")
    start_btn.grid(row=0, column=0, sticky="w")
    ttk.Button(btns, text="Open opslagmap",
               command=lambda: _open_folder(out_var.get())).grid(
        row=0, column=1, sticky="e")

    progress = ttk.Progressbar(main, mode="determinate")
    progress.grid(row=12, column=0, sticky="ew", pady=(8, 2))
    status_var = tk.StringVar(value="Gereed.")
    ttk.Label(main, textvariable=status_var).grid(row=13, column=0, sticky="w")

    main.rowconfigure(14, weight=1)
    logframe = ttk.Frame(main)
    logframe.grid(row=14, column=0, sticky="nsew", pady=(6, 0))
    logframe.columnconfigure(0, weight=1)
    logframe.rowconfigure(0, weight=1)
    logbox = tk.Text(logframe, height=12, wrap="word", state="disabled",
                     font=("Consolas" if sys.platform.startswith("win")
                           else "Monospace", 9))
    logbox.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(logframe, command=logbox.yview)
    sb.grid(row=0, column=1, sticky="ns")
    logbox.config(yscrollcommand=sb.set)

    # ---------- helpers ----------
    def append_log(msg: str):
        logbox.config(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.config(state="disabled")

    def collect_cfg() -> rc.Config:
        cfg.output_dir = out_var.get().strip() or rc.default_output_dir()
        cfg.slash_replacement = (slash_var.get() or "-")[:3]
        cfg.include_unlinked = bool(unlinked_var.get())
        cfg.generate_report = bool(report_var.get())
        cfg.overwrite_existing = bool(over_var.get())
        cfg.spaces_to_underscores = bool(under_var.get())
        try:
            cfg.request_delay = max(0.0, float(delay_var.get().replace(",", ".")))
        except ValueError:
            cfg.request_delay = 1.0
        return cfg

    def worker(srcs, c):
        def prog(done, total, ecli):
            work.put(("progress", done, total, ecli))

        def log(m):
            work.put(("log", m))
        try:
            harvest = rs.harvest_files(srcs)
            for name, msg in harvest.errors:
                work.put(("log", f"Let op: {name}: {msg}"))
            results = rc.process_entries(harvest.entries, c,
                                         progress_cb=prog, log_cb=log)
            report = ""
            if c.generate_report:
                report = rc.write_report(results, c.output_dir)
            work.put(("done", results, report))
        except Exception as e:
            work.put(("fatal", str(e)))

    def start():
        if not files:
            messagebox.showwarning(
                "Geen bron", "Voeg eerst een of meer bronbestanden toe.")
            return
        c = collect_cfg()
        try:
            c.save()
        except OSError:
            pass
        logbox.config(state="normal"); logbox.delete("1.0", "end")
        logbox.config(state="disabled")
        progress.config(value=0, maximum=100)
        status_var.set("Bezig\u2026")
        start_btn.config(state="disabled")
        threading.Thread(target=worker, args=(list(files), c),
                         daemon=True).start()

    start_btn.config(command=start)

    def poll():
        try:
            while True:
                item = work.get_nowait()
                kind = item[0]
                if kind == "log":
                    append_log(item[1])
                elif kind == "detect":
                    h = item[1]
                    counts.update(linked=h.n_linked, unlinked=h.n_unlinked,
                                  sum=h.summaries_found)
                    parts = [f"Gedetecteerd: {h.n_linked} met link",
                             f"{h.n_unlinked} zonder link"]
                    if h.summaries_found:
                        parts.append(f"{h.summaries_found} met samenvatting")
                    detect_var.set(" \u00b7 ".join(parts) + ".")
                    update_export_label()
                    for name, msg in h.errors:
                        append_log(f"Let op: {name}: {msg}")
                elif kind == "detect_err":
                    detect_var.set(f"Detectie mislukt: {item[1]}")
                elif kind == "progress":
                    _, done, total, ecli = item
                    progress.config(maximum=max(total, 1), value=done)
                    if total:
                        status_var.set(f"{done} / {total}    {ecli}")
                elif kind == "done":
                    _, results, report = item
                    s = _summarize(results)
                    status_var.set(
                        f"Klaar \u2014 volledige tekst: {s['ok']}, alleen "
                        f"metadata: {s['meta']}, overgeslagen: {s['skip']}, "
                        f"niet gelukt: {s['fail']}")
                    append_log("")
                    if report:
                        append_log(f"Rapport opgeslagen: {report}")
                    start_btn.config(state="normal")
                    msg = (f"Export voltooid.\n\n"
                           f"Volledige tekst : {s['ok']}\n"
                           f"Alleen metadata : {s['meta']}\n"
                           f"Met samenvatting: {s['sum']}\n"
                           f"Overgeslagen    : {s['skip']}\n"
                           f"Niet gelukt     : {s['fail']}\n")
                    if s["unlinked"]:
                        msg += f"\n{s['unlinked']} zonder hyperlink meegenomen."
                    if s["fallback"]:
                        msg += (f"\n{s['fallback']} bestand(en) kregen een titel "
                                f"uit zaaknummer/ECLI (zie rapport).")
                    messagebox.showinfo("Klaar", msg)
                elif kind == "fatal":
                    start_btn.config(state="normal")
                    status_var.set("Fout.")
                    messagebox.showerror("Fout", item[1])
        except queue.Empty:
            pass
        root.after(120, poll)

    if dnd:
        append_log("Sleep bronbestanden in het venster, of klik "
                   "'Bestanden toevoegen'.")
    else:
        append_log("Klik 'Bestanden toevoegen' om te beginnen. (Tip: sleep "
                   "bestanden op het programma-icoon om ze in te laden.)")
    append_log("De uitspraken worden opgehaald via Open Data van de Rechtspraak.")

    if initial_files:
        add_files(list(initial_files))

    root.after(120, poll)
    root.mainloop()
    return 0


# ===========================================================================
#  Entry point
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Exporteert uitspraken uit een uitsprakenoverzicht of een "
                    "ander bestand met ECLI-links als losse PDF's (met voorblad) "
                    "via Open Data van de Rechtspraak.")
    ap.add_argument("files", nargs="*",
                    help="bronbestand(en): PDF, .eml, .msg, .html of .txt")
    ap.add_argument("--pdf", help="(alias) pad naar een bronbestand")
    ap.add_argument("--run", action="store_true",
                    help="direct verwerken in de terminal (geen venster)")
    ap.add_argument("--out", help="opslagmap voor de PDF's")
    ap.add_argument("--slash", help='vervanging voor "/" in partijnamen (standaard "-")')
    ap.add_argument("--include-unlinked", action="store_true",
                    help="ook ECLI's zonder hyperlink (alleen tekst) meenemen")
    ap.add_argument("--no-report", action="store_true",
                    help="geen verwerkingsrapport schrijven")
    ap.add_argument("--overwrite", action="store_true",
                    help="bestaande bestanden overschrijven")
    ap.add_argument("--underscores", action="store_true",
                    help="spaties in bestandsnaam vervangen door underscores")
    ap.add_argument("--delay", type=float, help="pauze (sec) tussen verzoeken")
    ap.add_argument("--save-config", action="store_true",
                    help="de opgegeven instellingen bewaren als standaard")
    ap.add_argument("--gui", action="store_true", help="forceer grafische modus")
    args = ap.parse_args()

    files = list(args.files)
    if args.pdf:
        files.append(args.pdf)

    # Headless verwerken alleen als daar expliciet om wordt gevraagd.
    if args.run and not args.gui:
        return run_cli(files, args)
    if args.save_config and not files and not args.gui:
        return run_cli(files, args)

    try:
        return run_gui(initial_files=files)
    except Exception as e:
        print(f"Grafische modus niet beschikbaar ({e}).", file=sys.stderr)
        print("Gebruik de opdrachtregel, bijvoorbeeld:", file=sys.stderr)
        print('  python rechtspraak_downloader.py --run "overzicht.pdf" '
              '--out "D:/Uitspraken"', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
