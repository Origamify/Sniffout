(function () {
  var article = document.getElementById("zone-article");
  // The active wiki is the first URL path segment; all wiki routes are prefixed with it.
  var segs = location.pathname.split("/");
  var wiki = segs[1] || "";
  var searchForm = document.querySelector('form[action$="/search"]');
  var ingestForm = document.querySelector('form[action$="/ingest"]');
  var chatForm = document.querySelector('form[action$="/agent-request"]');
  var createForm = document.querySelector('form[action="/wiki/create"]');

  // Jobs list: poll every couple of seconds and color by state (running green,
  // queued orange, done dim, failed red). Shows jobs from every wiki.
  var jobsList = document.getElementById("jobs-list");
  if (jobsList) {
    function renderJobs(jobs) {
      jobsList.textContent = "";
      if (!jobs.length) {
        var empty = document.createElement("li");
        empty.className = "job-none";
        empty.textContent = "No jobs yet.";
        jobsList.appendChild(empty);
        return;
      }
      jobs.forEach(function (job) {
        var li = document.createElement("li");
        li.className = "job job-" + job.state;
        var dot = document.createElement("span");
        dot.className = "job-dot";
        var label = document.createElement("span");
        label.className = "job-id";
        // ids carry the wiki: sniffout-<wiki slug>-<timestamp>
        label.textContent = job.job_id;
        var state = document.createElement("span");
        state.className = "job-state";
        state.textContent = job.state;
        li.appendChild(dot);
        li.appendChild(label);
        li.appendChild(state);
        jobsList.appendChild(li);
      });
    }

    function pollJobs() {
      fetch("/jobs")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data) renderJobs(data.jobs); })
        .catch(function () { /* server hiccup: keep polling */ })
        .finally(function () { setTimeout(pollJobs, 2000); });
    }
    pollJobs();
  }

  // Disable the submit button and show progress while an async form runs,
  // so double-clicks can't fire the request twice.
  async function withBusy(form, label, run) {
    var btn = form.querySelector("button");
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = label;
    try {
      await run();
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  function buildResultList(results) {
    var count = document.createElement("p");
    count.className = "results-count";
    count.textContent = results.length + (results.length === 1 ? " result" : " results");
    var list = document.createElement("ul");
    list.className = "results-list";
    results.forEach(function (entry, i) {
      var li = document.createElement("li");
      li.className = "result-entry";
      var rank = document.createElement("span");
      rank.className = "result-rank";
      rank.textContent = String(i + 1);
      var main = document.createElement("div");
      main.className = "result-main";
      var link = document.createElement("a");
      link.href = entry.url;
      link.textContent = entry.title;
      var urlP = document.createElement("p");
      urlP.className = "result-url";
      if (entry.domain) {
        var domain = document.createElement("span");
        domain.className = "result-domain";
        domain.textContent = entry.domain;
        urlP.appendChild(domain);
      }
      urlP.appendChild(document.createTextNode(entry.url));
      var snippetP = document.createElement("p");
      snippetP.className = "result-snippet";
      snippetP.textContent = entry.snippet;
      main.appendChild(link);
      main.appendChild(urlP);
      main.appendChild(snippetP);
      li.appendChild(rank);
      li.appendChild(main);
      if (entry.url) {
        var ingest = document.createElement("button");
        ingest.type = "button";
        ingest.className = "result-ingest";
        ingest.textContent = "Ingest";
        ingest.dataset.url = entry.url;
        li.appendChild(ingest);
      }
      list.appendChild(li);
    });
    return [count, list];
  }

  if (searchForm) {
    searchForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      await withBusy(searchForm, "Searching…", async function () {
        var formData = new FormData(searchForm);
        var resp = await fetch("/" + wiki + "/search", {
          method: "POST",
          body: formData,
          headers: { "Accept": "application/json" },
        });
        var data = await resp.json();
        article.querySelectorAll(".results-list, .results-count, .search-error, .empty-state").forEach(function (el) {
          el.remove();
        });
        if (Array.isArray(data.results)) {
          buildResultList(data.results).forEach(function (el) {
            article.appendChild(el);
          });
        } else if (typeof data.error === "string") {
          var errP = document.createElement("p");
          errP.className = "search-error";
          errP.textContent = data.error;
          article.appendChild(errP);
        }
      });
    });
  }

  if (ingestForm) {
    article.addEventListener("click", function (event) {
      var btn = event.target.closest(".result-ingest");
      if (!btn || !btn.dataset.url) return;
      ingestForm.elements["url"].value = btn.dataset.url;
      ingestForm.scrollIntoView({ behavior: "smooth", block: "center" });
      ingestForm.requestSubmit();
    });

    ingestForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      await withBusy(ingestForm, "Ingesting…", async function () {
        var formData = new FormData(ingestForm);
        var resp = await fetch("/" + wiki + "/ingest", { method: "POST", body: formData });
        var text = await resp.text();
        article.querySelectorAll(".ingest-result").forEach(function (el) {
          el.remove();
        });
        var p = document.createElement("p");
        p.className = "ingest-result " + (text.startsWith("Stored") ? "ingest-ok" : "ingest-fail");
        p.textContent = text;
        // Feedback belongs next to the form it came from, not the article bottom
        ingestForm.insertAdjacentElement("afterend", p);
      });
    });
  }

  if (createForm) {
    // Inline errors instead of leaving the picker for a plain-text 400 page
    createForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var errEl = document.getElementById("create-error");
      errEl.hidden = true;
      await withBusy(createForm, "Creating…", async function () {
        var resp = await fetch(createForm.getAttribute("action"), {
          method: "POST",
          body: new FormData(createForm),
        });
        if (resp.redirected || resp.ok) {
          location.href = resp.url;
          return;
        }
        errEl.textContent = await resp.text();
        errEl.hidden = false;
      });
    });
  }

  if (chatForm) {
    var statusEl = document.getElementById("status-content");
    var pollGen = 0; // bump to cancel the watch loop of a superseded job

    function renderResult(data) {
      statusEl.textContent = "";
      // The researcher's final message is the headline; the content-event
      // stream (interim chatter) is only a fallback.
      if (typeof data.output === "string" && data.output.trim()) {
        var out = document.createElement("div");
        out.className = "event";
        out.innerHTML = data.output;
        statusEl.appendChild(out);
      } else if (Array.isArray(data.events)) {
        data.events.forEach(function (evt) {
          var div = document.createElement("div");
          div.className = "event";
          div.innerHTML = evt.body;
          statusEl.appendChild(div);
        });
      } else if (typeof data.content === "string") {
        var p = document.createElement("p");
        p.textContent = data.content;
        statusEl.appendChild(p);
      }
    }

    function resultUrl(file) {
      return "/" + wiki + "/result/" + encodeURIComponent(file);
    }

    function fetchResult(file, render) {
      fetch(resultUrl(file))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data) render(data); });
    }

    // One poller at a time: /job/active answers instantly from the job map;
    // done means the result file was published — fetch and render it.
    function waitOnJob(jobId) {
      var gen = ++pollGen;
      statusEl.textContent = "Running job " + jobId + "\u2026";
      async function poll() {
        if (gen !== pollGen) return; // superseded by a newer job
        try {
          var resp = await fetch("/" + wiki + "/job/active");
          if (!resp.ok) throw new Error("bad status");
          var data = await resp.json();
          if (data.done) {
            fetchResult(data.job_id + ".out", renderResult);
            return;
          }
          if (data.job_id === null) return; // superseded server-side
        } catch (e) { /* server hiccup: keep polling */ }
        setTimeout(poll, 2000);
      }
      poll();
    }

    // Reload resume: ask the server about the wiki's active job (instant,
    // from its job map) — if one is in flight, watch it with the wait loop.
    function resumeActiveJob() {
      fetch("/" + wiki + "/job/active")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          if (data.done) {
            fetchResult(data.job_id + ".out", renderResult);
          } else if (data.job_id === null) {
            // Nothing in flight: show the latest stored result once.
            fetch("/" + wiki + "/result/latest")
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (res) { if (res) renderResult(res); });
          } else {
            waitOnJob(data.job_id);
          }
        });
    }

    chatForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      await withBusy(chatForm, "Sending…", async function () {
        var formData = new FormData(chatForm);
        var resp = await fetch("/" + wiki + "/agent-request", { method: "POST", body: formData });
        var text = await resp.text();
        chatForm.elements["message"].value = "";
        chatForm.querySelectorAll(".chat-result").forEach(function (el) {
          el.remove();
        });
        var data = null;
        try { data = JSON.parse(text); } catch (e) { /* dispatch error text */ }
        if (data && typeof data.job_id === "string") {
          // Watch this run: the poll loop renders the result the moment the
          // worker publishes it.
          waitOnJob(data.job_id);
        } else {
          var p = document.createElement("p");
          p.className = "chat-result";
          p.textContent = text;
          chatForm.appendChild(p);
        }
      });
    });

    document.getElementById("chat-message").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        chatForm.requestSubmit();
      }
    });

    // --- loop controls (Start/Stop Loop, Max runs) ---------------------------

    var startBtn = document.getElementById("start-loop");
    var stopBtn = document.getElementById("stop-loop");

    function loopUrl(action) {
      return "/" + wiki + "/loop/" + action;
    }

    function note(text) {
      // One transient feedback line under the form (the class the chat error
      // path uses; the next chat send clears it).
      chatForm.querySelectorAll(".chat-result").forEach(function (el) {
        el.remove();
      });
      var p = document.createElement("p");
      p.className = "chat-result";
      p.textContent = text;
      chatForm.appendChild(p);
    }

    function setLoopButtons(state) {
      var running = !!(state && state.status === "running");
      startBtn.disabled = running;
      stopBtn.disabled = !running;
    }

    // The final report — it carries the appended "Loop stopped: <reason>" note.
    function renderFinal() {
      fetch("/" + wiki + "/result/latest")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data) renderResult(data); });
    }

    function watchLoop() {
      // Follow the loop's status until it stops, then render the final
      // report once. Only one watcher can exist: Start is disabled while
      // a loop runs, and a stopped watcher has exited.
      async function poll() {
        try {
          var resp = await fetch(loopUrl("state"));
          if (resp.ok) {
            var state = await resp.json();
            setLoopButtons(state);
            if (!state || state.status !== "running") {
              if (state && state.status === "stopped") renderFinal();
              return;
            }
          }
        } catch (e) { /* server hiccup: keep polling */ }
        setTimeout(poll, 2000);
      }
      poll();
    }

    startBtn.addEventListener("click", async function () {
      startBtn.disabled = true;
      startBtn.textContent = "Starting\u2026";
      var started = false;
      try {
        var resp = await fetch(loopUrl("start"), {
          method: "POST",
          body: new FormData(chatForm), // message + max_runs
        });
        var text = await resp.text();
        if (resp.ok) {
          started = true;
          chatForm.elements["message"].value = "";
          setLoopButtons(JSON.parse(text));
          note("Loop started \u2014 the final report appears below when the loop stops.");
          watchLoop();
        } else {
          note(text || "Start Loop failed.");
        }
      } catch (e) {
        note("Start Loop failed: " + e);
      } finally {
        startBtn.textContent = "Start Loop";
        if (!started) startBtn.disabled = false;
      }
    });

    stopBtn.addEventListener("click", async function () {
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping\u2026";
      try {
        var resp = await fetch(loopUrl("stop"), { method: "POST" });
        if (resp.ok) {
          var state = await resp.json();
          setLoopButtons(state);
          stopBtn.textContent = "Stop Loop";
          note(state && state.status === "running"
            ? "Stop requested \u2014 the running run aborts at its next round boundary."
            : "No loop running on this wiki.");
        } else {
          note((await resp.text()) || "Stop Loop failed.");
          stopBtn.textContent = "Stop Loop";
          stopBtn.disabled = false;
        }
      } catch (e) {
        note("Stop Loop failed: " + e);
        stopBtn.textContent = "Stop Loop";
        stopBtn.disabled = false;
      }
    });

    // Buttons reflect a loop that is still active after a page reload.
    fetch(loopUrl("state"))
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) {
        if (!state) return;
        setLoopButtons(state);
        if (state.status === "running") watchLoop();
      });

    // Resume the run that is still in flight after a page reload.
    resumeActiveJob();
  }
})();

(function () {
  var MIN = 180;
  var MAX = 640;
  document.querySelectorAll(".resizer").forEach(function (el) {
    var side = el.dataset.side; // "left" | "right"
    var prop = "--" + side + "-col";
    var panel = document.getElementById(side === "left" ? "zone-index" : "zone-status");
    var stored = localStorage.getItem("sniffout-" + side + "-col");
    if (stored) document.documentElement.style.setProperty(prop, stored);

    el.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      var startX = e.clientX;
      var startW = panel.getBoundingClientRect().width;
      el.setPointerCapture(e.pointerId);
      el.classList.add("dragging");

      function move(ev) {
        var delta = side === "left" ? ev.clientX - startX : startX - ev.clientX;
        var w = Math.max(MIN, Math.min(MAX, startW + delta));
        document.documentElement.style.setProperty(prop, w + "px");
      }
      function up() {
        el.releasePointerCapture(e.pointerId);
        el.classList.remove("dragging");
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        localStorage.setItem("sniffout-" + side + "-col",
          getComputedStyle(document.documentElement).getPropertyValue(prop));
      }
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
    });
  });
})();
