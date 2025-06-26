import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
import threading
from queue import Queue, Empty
import backend
import webbrowser
import re
from ttkbootstrap import Style

RELATED_LIMIT = 4
TYPE_SPEED_MS = 1

class ResearchAssistantApp:
    def __init__(self, root):
        style = Style(theme="cyborg")
        self.style = style

        self.root = root
        self.root.title("AI Research Assistant")
        self.root.geometry("950x750")
        self.root.protocol("WM_DELETE_WINDOW", self._quit_app)

        self.queue = Queue()
        self.limit_var = tk.IntVar(value=7)
        self.papers = []
        self.fetching_search = False
        self.fetching_related = False
        self.active_toplevels = {}
        self.typing_jobs = {}
        self.displayed_ids = set()
        self.current_paper_id = None

        top = ttk.Frame(root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Topic:", width=8).pack(side=tk.LEFT, padx=(0,5))
        self.entry = ttk.Entry(top, bootstyle="info")
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,6))
        ttk.Label(top, text="Results:", width=8).pack(side=tk.LEFT, padx=(6,2))
        spin = tk.Spinbox(
            top,
            from_=1, to=15, width=4,
            textvariable=self.limit_var,
            validate="all",
            validatecommand=(top.register(lambda v: v.isdigit() and 1 <= int(v) <= 15), "%P")
        )
        spin.pack(side=tk.LEFT, padx=(0,6))
        self.search_button = ttk.Button(
            top, text="Search", bootstyle="success-outline", command=self.start_search
        )
        self.search_button.pack(side=tk.LEFT)
        self.entry.bind("<Return>", lambda e: self.start_search())

        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0,10))

        lf = ttk.LabelFrame(paned, text="Found Papers", bootstyle="info", padding=5)
        self.listbox = tk.Listbox(
            lf, activestyle="none", exportselection=False,
            bg=style.colors.dark, fg=style.colors.light,
            selectbackground=style.colors.info, selectforeground=style.colors.dark
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_listbox_select)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview, bootstyle="info-round")
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.config(yscrollcommand=sb.set)
        paned.add(lf, weight=1)

        rf = ttk.LabelFrame(paned, text="Details & Insights", bootstyle="info", padding=5)
        rf.columnconfigure(0, weight=1)
        rf.rowconfigure(1, weight=1)

        self.details_title_label = ttk.Label(
            rf, text="", font=("Segoe UI", 10, "bold"),
            anchor=tk.W, bootstyle="inverse-info"
        )
        self.details_title_label.grid(row=0, column=0, sticky="ew", pady=(0,5))

        self.text = ScrolledText(
            rf, wrap=tk.WORD, state=tk.DISABLED,
            bg=style.colors.secondary, fg=style.colors.dark
        )
        self.text.grid(row=1, column=0, sticky="nsew")
        paned.add(rf, weight=3)

        self.status = ttk.Label(
            root,
            text="Enter a topic and hit Search. Insights are AI-generated—verify before trusting",
            anchor=tk.W, padding=(10,3), bootstyle="light"
        )
        self.status.pack(fill=tk.X)

        self.text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("link", underline=True, foreground=style.colors.info)
        self.text.tag_configure("clickable_title", underline=True, foreground=style.colors.success)
        self.text.tag_configure("italic_grey", font=("Segoe UI", 9, "italic"), foreground=style.colors.light)
        self.text.tag_configure("error", foreground=style.colors.danger)
        for tag in ("link", "clickable_title"):
            self.text.tag_bind(tag, "<Enter>", self._enter_link)
            self.text.tag_bind(tag, "<Leave>", self._leave_link)
            self.text.tag_bind(tag, "<Button-1>", self._click_handler)

        self.check_queue()


    def _quit_app(self):
        for job in list(self.typing_jobs.values()):
            if job:
                try: self.root.after_cancel(job)
                except: pass
        for win in list(self.active_toplevels.values()):
            if win.winfo_exists():
                win.destroy()
        self.root.destroy()


    def _enter_link(self, event):
        event.widget.config(cursor="hand2")


    def _leave_link(self, event):
        event.widget.config(cursor="")


    def _click_handler(self, event):
        if self.fetching_related:
            self.update_status("Busy fetching…")
            return

        idx = event.widget.index(f"@{event.x},{event.y}")
        tags = event.widget.tag_names(idx)
        line = event.widget.get(f"{idx} linestart", f"{idx} lineend")

        if "link" in tags and event.widget == self.text:
            rng = event.widget.tag_prevrange("link", idx)
            if rng:
                url = event.widget.get(rng[0], rng[1])
                webbrowser.open_new_tab(url)
            return

        if "clickable_title" in tags and event.widget == self.text:
            m = re.search(r"\[(.*?)\]", line)
            if not m:
                messagebox.showwarning("Parsing Error", "Could not extract Paper ID.")
                return
            pid = m.group(1)
            if pid in self.active_toplevels:
                win = self.active_toplevels[pid]
                if win.winfo_exists():
                    win.lift(); win.focus_force()
                    return

            self.fetching_related = True
            self.search_button.config(state=tk.DISABLED)
            threading.Thread(
                target=backend.fetch_paper_details_backend,
                args=(pid, self.queue),
                daemon=True
            ).start()


    def update_status(self, msg):
        if self.status.winfo_exists():
            self.status.config(text=msg)


    def start_search(self):
        if self.fetching_search or self.fetching_related:
            messagebox.showwarning("Busy", "Please wait until current operation completes.")
            return

        topic = self.entry.get().strip()
        if not topic:
            messagebox.showwarning("Input Error", "Please enter a topic.")
            return

        self.listbox.delete(0, tk.END)
        self.text.config(state=tk.NORMAL); self.text.delete("1.0", tk.END); self.text.config(state=tk.DISABLED)
        self.details_title_label.config(text="Details & Insights")
        self.papers.clear(); self.displayed_ids.clear(); self.current_paper_id = None

        self.update_status(f"Searching for '{topic}'…")
        self.fetching_search = True
        self.search_button.config(state=tk.DISABLED)
        threading.Thread(
            target=backend.search_papers_backend,
            args=(topic, self.limit_var.get(), self.queue),
            daemon=True
        ).start()


    def _cancel_typing(self, widget):
        wid = str(widget)
        job = self.typing_jobs.pop(wid, None)
        if job:
            try: widget.after_cancel(job)
            except: pass
        if widget.winfo_exists() and widget["state"] == tk.NORMAL:
            widget.config(state=tk.DISABLED)


    def _type_text(self, widget, text, pid, idx=0):
        wid = str(widget)
        if not widget.winfo_exists():
            self.typing_jobs.pop(wid, None)
            return

        if idx == 0:
            prev = self.typing_jobs.get(wid)
            if prev:
                try: widget.after_cancel(prev)
                except: pass
            self.typing_jobs[wid] = None
            if widget["state"] == tk.DISABLED:
                widget.config(state=tk.NORMAL)

            hdr = "--- Gemini Insights/Essay ---\n"
            if widget is not self.text:
                hdr = "--- Gemini Analytical Essay ---\n"
            pos = widget.search(hdr, "1.0", stopindex=tk.END)
            if pos:
                widget.delete(f"{pos} lineend+1c", tk.END)
                widget.insert(tk.END, "\n")
            else:
                widget.delete("1.0", tk.END)
                widget.insert("1.0", hdr + "\n")
            self.displayed_ids.add(pid)

        if idx < len(text):
            widget.insert(tk.END + "-1c", text[idx])
            widget.see(tk.END)
            job = widget.after(TYPE_SPEED_MS, self._type_text, widget, text, pid, idx+1)
            self.typing_jobs[wid] = job
        else:
            widget.insert(tk.END, "\n\n")
            widget.config(state=tk.DISABLED)
            self.typing_jobs.pop(wid, None)


    def _insert_formatted_instantly(self, widget, pid, content):
        self._cancel_typing(widget)
        if not widget.winfo_exists(): return
        widget.config(state=tk.NORMAL)

        hdr = "--- Gemini Insights/Essay ---\n"
        if widget is not self.text:
            hdr = "--- Gemini Analytical Essay ---\n"
        pos = widget.search(hdr, "1.0", stopindex=tk.END)
        if pos:
            widget.delete(f"{pos} lineend+1c", tk.END)
            ins = widget.index(f"{pos} lineend+1c +1c")
        else:
            widget.delete("1.0", tk.END)
            widget.insert("1.0", hdr + "\n")
            ins = "2.0"

        pattern = re.compile(r"(\*\*(.*?)\*\*)|(\*(.*?)\*)")
        last = 0
        for m in pattern.finditer(content):
            start, end = m.span()
            plain = content[last:start]
            if plain:
                widget.insert(ins, plain)
                ins = widget.index(f"{ins} +{len(plain)}c")

            if m.group(1):
                tag, txt = "bold", m.group(2)
            else:
                tag, txt = "italic_grey", m.group(4)
            widget.insert(ins, txt, (tag,))
            ins = widget.index(f"{ins} +{len(txt)}c")
            last = end

        tail = content[last:]
        if tail:
            widget.insert(ins, tail)

        widget.insert(tk.END, "\n\n")
        widget.config(state=tk.DISABLED)
        self.displayed_ids.add(pid)


    def _populate_main_details_widgets(self, widget, data, skip_typing=False):
        pid = data.get("paperId")
        self._cancel_typing(widget)
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)

        def detail(lbl, val):
            widget.insert(tk.END, f"{lbl}: ", ("bold",))
            widget.insert(tk.END, f"{val}\n")

        detail("Title", data.get("title","N/A"))
        authors = ", ".join(a.get("name","N/A") for a in data.get("authors",[]))
        detail("Authors", authors)
        detail("Year", data.get("year","N/A"))
        detail("Venue", data.get("venue","N/A"))

        if data.get("citationCount", 0):
            detail("Citations", data["citationCount"])
        if data.get("influentialCitationCount", 0):
            detail("Influential Citations", data["influentialCitationCount"])

        url = data.get("url","")
        widget.insert(tk.END, "URL: ", ("bold",))
        if url.startswith("http"):
            start = widget.index(tk.INSERT)
            widget.insert(tk.END, url + "\n")
            end = widget.index(tk.INSERT)
            widget.tag_add("link", start, end)
        else:
            widget.insert(tk.END, (url or "N/A") + "\n")

        widget.insert(tk.END, "\n--- Abstract ---\n", ("bold",))
        widget.insert(tk.END, data.get("abstract","N/A") + "\n\n")
        widget.insert(tk.END, "--- Gemini Insights/Essay ---\n", ("bold",))

        insights = data.get("insights","")
        is_err = insights.startswith("Error:") or insights.startswith("Content blocked")
        if is_err:
            widget.insert(tk.END, insights + "\n\n", ("error",))
            self.displayed_ids.add(pid)
        elif skip_typing:
            self._insert_formatted_instantly(widget, pid, insights)
        else:
            widget.config(state=tk.DISABLED)
            self.root.after(50, self._type_text, widget, insights, pid, 0)

        widget.config(state=tk.NORMAL)
        widget.insert(tk.END, "\n--- Related Work (click title) ---\n", ("bold",))
        related = data.get("references",[]) + data.get("citations",[])
        shown = 0
        for itm in related:
            if shown >= RELATED_LIMIT: break
            rid, title = itm.get("paperId"), itm.get("title","N/A")
            widget.insert(tk.END, f"  • [{rid}] ")
            start = widget.index(tk.INSERT)
            widget.insert(tk.END, title + "\n")
            end = widget.index(tk.INSERT)
            if rid:
                widget.tag_add("clickable_title", start, end)
            shown += 1

        widget.config(state=tk.DISABLED)


    def _populate_related_window_widgets(self, widget, data):
        pid = data.get("paperId")
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, "--- Abstract ---\n", ("bold",))
        widget.insert(tk.END, data.get("abstract","N/A") + "\n\n")
        widget.insert(tk.END, "--- Gemini Analytical Essay ---\n", ("bold",))

        insights = data.get("insights","")
        if insights.startswith("Error:") or insights.startswith("Content blocked"):
            widget.insert(tk.END, insights + "\n\n", ("error",))
            widget.config(state=tk.DISABLED)
            self.displayed_ids.add(pid)
        else:
            widget.config(state=tk.DISABLED)
            self.root.after(50, self._type_text, widget, insights, pid, 0)


    def check_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg is None:
                    self.fetching_search = False
                    if not self.fetching_related:
                        self.search_button.config(state=tk.NORMAL)
                    self.update_status("Search Finished")
                else:
                    kind, data = msg
                    if kind == "status":
                        self.update_status(data)
                    elif kind == "papers":
                        self.fetching_search = False
                        self.search_button.config(state=tk.NORMAL)
                        self.papers = data
                        self.listbox.delete(0, tk.END)
                        for i,p in enumerate(data,1):
                            year = p.get("year","N/A")
                            title = p.get("title","N/A")[:70]
                            self.listbox.insert(tk.END, f"{i}. ({year}) {title}")
                        if self.papers:
                            self.listbox.selection_set(0)
                            self.display_main_paper_details(self.papers[0])
                        else:
                            self.update_status("No papers found.")
                    elif kind == "paper_details":
                        self.fetching_related = False
                        self.search_button.config(state=tk.NORMAL)
                        self.show_related_paper_window(data)
                    elif kind == "paper_details_error":
                        self.fetching_related = False
                        self.search_button.config(state=tk.NORMAL)
                        messagebox.showerror("Fetch Error", f"Could not fetch details:\n{data}")
        except Empty:
            pass
        finally:
            self.root.after(100, self.check_queue)


    def on_listbox_select(self, event):
        if self.fetching_search or self.fetching_related:
            return
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < len(self.papers):
            pid = self.papers[idx].get("paperId")
            if pid and pid != self.current_paper_id:
                self.display_main_paper_details(self.papers[idx])


    def display_main_paper_details(self, data):
        pid = data.get("paperId")
        if not pid:
            self.text.config(state=tk.NORMAL)
            self.text.delete("1.0", tk.END)
            self.text.insert("1.0", "Error: Missing ID.")
            self.text.config(state=tk.DISABLED)
            return
        skip = pid in self.displayed_ids
        self.current_paper_id = pid
        title = data.get("title","N/A")
        self.details_title_label.config(text=f"Details for: {title[:80]}…")
        self._populate_main_details_widgets(self.text, data, skip_typing=skip)


    def show_related_paper_window(self, data):
        pid = data.get("paperId")
        if not pid:
            messagebox.showerror("Error", "Invalid data for related paper.")
            return
        if pid in self.active_toplevels:
            win = self.active_toplevels[pid]
            if win.winfo_exists():
                win.lift(); win.focus_force()
                return
        if len(self.active_toplevels) >= 5:
            messagebox.showwarning("Window Limit", "Close some related windows first.")
            return

        top = tk.Toplevel(self.root)
        top.title(f"Related: {data.get('title','…')[:60]}…")
        top.geometry("700x600")
        top.transient(self.root)
        self.active_toplevels[pid] = top
        top.protocol("WM_DELETE_WINDOW", lambda w=top,p=pid: self._close_toplevel(w,p))

        frm = ttk.Frame(top, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.rowconfigure(0, weight=1); frm.columnconfigure(0, weight=1)

        txt = ScrolledText(frm, wrap=tk.WORD, state=tk.DISABLED)
        txt.grid(row=0, column=0, sticky="nsew")

        self._populate_related_window_widgets(txt, data)


    def _close_toplevel(self, win, pid):
        try:
            txt = win.winfo_children()[0].winfo_children()[0]
            self._cancel_typing(txt)
        except:
            pass
        self.active_toplevels.pop(pid, None)
        if win.winfo_exists():
            win.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ResearchAssistantApp(root)
    root.mainloop()
