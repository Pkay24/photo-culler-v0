/* Photo Culler v0 - keyboard-driven review.
   The keyboard is the primary interface; gestures are aliases, never the layout driver. */

(function () {
  "use strict";

  var PRELOAD_AHEAD = 8;
  var PRELOAD_BEHIND = 2;
  var DPI = 300;

  var photos = [];
  var byId = {};
  var index = 0;
  var counts = { keep: 0, maybe: 0, reject: 0, undecided: 0 };
  var undoStack = [];
  var preloaded = {};
  var zoomOpen = false;
  var cursor = { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  var toastTimer = null;
  var positionTimer = null;

  var el = {
    photo: document.getElementById("photo"),
    flash: document.getElementById("flash"),
    filename: document.getElementById("filename"),
    dims: document.getElementById("dims"),
    print: document.getElementById("print"),
    raw: document.getElementById("raw"),
    position: document.getElementById("position"),
    nKeep: document.getElementById("n-keep"),
    nMaybe: document.getElementById("n-maybe"),
    nReject: document.getElementById("n-reject"),
    progress: document.getElementById("progress-fill"),
    zoom: document.getElementById("zoom"),
    zoomImg: document.getElementById("zoom-img"),
    modal: document.getElementById("modal"),
    dest: document.getElementById("dest"),
    modalSummary: document.getElementById("modal-summary"),
    modalError: document.getElementById("modal-error"),
    exportGo: document.getElementById("export-go"),
    exportCancel: document.getElementById("export-cancel"),
    toast: document.getElementById("toast")
  };

  // ---- server -------------------------------------------------------------

  function getJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }

  // Decisions are written as they happen, but the UI never waits on the write.
  var writeChain = Promise.resolve();
  function queueDecision(id, decision) {
    writeChain = writeChain.then(function () {
      return postJSON("/api/decide", { id: id, decision: decision });
    }).catch(function (err) {
      toast("Could not save decision: " + err.message);
    });
  }

  function savePosition() {
    if (positionTimer) clearTimeout(positionTimer);
    positionTimer = setTimeout(function () {
      postJSON("/api/position", { seq: index }).catch(function () {});
    }, 120);
  }

  // ---- rendering ----------------------------------------------------------

  function proxyURL(photo) { return "/proxy/" + photo.id; }

  function printSize(photo) {
    if (!photo.width || !photo.height) return "";
    var w = photo.width / DPI;
    var h = photo.height / DPI;
    return w.toFixed(1) + " × " + h.toFixed(1) + " in";
  }

  function render() {
    var photo = photos[index];
    if (!photo) return;

    el.photo.classList.add("loading");
    el.photo.src = proxyURL(photo);

    el.filename.textContent = photo.name;
    el.dims.textContent = (photo.width && photo.height)
      ? photo.width + " × " + photo.height
      : "";
    el.print.textContent = printSize(photo) + " @ " + DPI + "dpi";
    // A frame that cannot hold a 12in edge is worth flagging before you decide.
    var longEdgeIn = Math.max(photo.width || 0, photo.height || 0) / DPI;
    el.print.classList.toggle("small", longEdgeIn > 0 && longEdgeIn < 12);
    el.raw.classList.toggle("hidden", !photo.has_raw);
    if (photo.has_raw) el.raw.title = photo.raw_name || "RAW attached";

    el.position.textContent = (index + 1) + " / " + photos.length
      + (photo.decision !== "undecided" ? "  · " + photo.decision : "");
    el.progress.style.width = (photos.length
      ? ((index + 1) / photos.length * 100) : 0) + "%";

    renderCounts();
    preload();
    savePosition();
    if (zoomOpen) loadZoom();
  }

  function renderCounts() {
    el.nKeep.textContent = counts.keep;
    el.nMaybe.textContent = counts.maybe;
    el.nReject.textContent = counts.reject;
  }

  el.photo.addEventListener("load", function () {
    el.photo.classList.remove("loading");
  });

  function preload() {
    for (var i = index - PRELOAD_BEHIND; i <= index + PRELOAD_AHEAD; i++) {
      if (i < 0 || i >= photos.length || i === index) continue;
      var photo = photos[i];
      if (preloaded[photo.id]) continue;
      var img = new Image();
      img.src = proxyURL(photo);
      preloaded[photo.id] = img;
    }
  }

  function flash(kind) {
    el.flash.className = kind + " on";
    setTimeout(function () { el.flash.className = kind; }, 60);
  }

  function toast(message, ms) {
    el.toast.textContent = message;
    el.toast.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.classList.add("hidden");
    }, ms || 2600);
  }

  // ---- navigation and decisions ------------------------------------------

  function go(newIndex) {
    index = Math.max(0, Math.min(newIndex, photos.length - 1));
    render();
  }

  function applyDecision(photo, decision) {
    if (photo.decision === decision) return;
    counts[photo.decision] = Math.max(0, counts[photo.decision] - 1);
    counts[decision] = (counts[decision] || 0) + 1;
    photo.decision = decision;
    queueDecision(photo.id, decision);
  }

  function decide(decision) {
    var photo = photos[index];
    if (!photo) return;
    undoStack.push({ index: index, id: photo.id, previous: photo.decision });
    applyDecision(photo, decision);
    flash(decision);
    if (index >= photos.length - 1) {
      render();
      toast("End of the set. " + counts.keep + " keep, " + counts.maybe
        + " maybe, " + counts.reject + " reject. Press E to export, "
        + "or Shift+← to revisit.");
    } else {
      go(index + 1);
    }
  }

  function undo() {
    var entry = undoStack.pop();
    if (!entry) {
      toast("Nothing to undo in this session. Use Shift+← to step back "
        + "through photos you already decided.");
      return;
    }
    var photo = byId[entry.id];
    if (photo) applyDecision(photo, entry.previous);
    flash("undo");
    go(entry.index);
  }

  // Browsing, as distinct from deciding. Undo only reaches decisions made in
  // this page's lifetime; these two reach every photo, including on a re-run
  // that resumes at the end of the set.
  function back() {
    if (index <= 0) {
      toast("Start of the set.");
      return;
    }
    go(index - 1);
  }

  function forward() {
    if (index >= photos.length - 1) {
      toast("End of the set.");
      return;
    }
    go(index + 1);
  }

  function skip() {
    forward();
  }

  // ---- full-resolution zoom (Z held) -------------------------------------

  function placeZoom() {
    var img = el.zoomImg;
    var nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return;
    var vw = window.innerWidth, vh = window.innerHeight;

    // Where the cursor sits over the fit-to-window photo, as a 0..1 fraction.
    var rect = el.photo.getBoundingClientRect();
    var fx = rect.width ? (cursor.x - rect.left) / rect.width : 0.5;
    var fy = rect.height ? (cursor.y - rect.top) / rect.height : 0.5;
    fx = Math.max(0, Math.min(1, fx));
    fy = Math.max(0, Math.min(1, fy));

    img.style.left = (nw <= vw ? (vw - nw) / 2 : -fx * (nw - vw)) + "px";
    img.style.top = (nh <= vh ? (vh - nh) / 2 : -fy * (nh - vh)) + "px";
  }

  function loadZoom() {
    var photo = photos[index];
    if (!photo) return;
    var url = "/full/" + photo.id;
    if (el.zoomImg.getAttribute("data-url") !== url) {
      el.zoomImg.setAttribute("data-url", url);
      el.zoomImg.src = url;
    } else {
      placeZoom();
    }
  }

  el.zoomImg.addEventListener("load", placeZoom);

  function openZoom() {
    if (zoomOpen) return;
    zoomOpen = true;
    el.zoom.classList.remove("hidden");
    loadZoom();
  }

  function closeZoom() {
    if (!zoomOpen) return;
    zoomOpen = false;
    el.zoom.classList.add("hidden");
  }

  document.addEventListener("mousemove", function (ev) {
    cursor.x = ev.clientX;
    cursor.y = ev.clientY;
    if (zoomOpen) placeZoom();
  });

  // ---- export -------------------------------------------------------------

  function openExport() {
    el.modalError.classList.add("hidden");
    el.modalSummary.innerHTML =
      "Will copy <b>" + counts.keep + "</b> keep and <b>" + counts.maybe +
      "</b> maybe (plus attached RAWs). <b>" + counts.reject +
      "</b> reject and <b>" + counts.undecided +
      "</b> undecided stay where they are.";
    el.modal.classList.remove("hidden");
    el.dest.focus();
    el.dest.select();
  }

  function closeExport() {
    el.modal.classList.add("hidden");
    el.exportGo.disabled = false;
    el.exportGo.textContent = "Export";
  }

  function runExport() {
    var dest = el.dest.value.trim();
    if (!dest) {
      el.modalError.textContent = "Enter a destination folder.";
      el.modalError.classList.remove("hidden");
      return;
    }
    el.exportGo.disabled = true;
    el.exportGo.textContent = "Copying…";
    el.modalError.classList.add("hidden");
    postJSON("/api/export", { dest: dest }).then(function (data) {
      var c = data.report.counts;
      closeExport();
      toast("Exported " + c.files_copied + " photo(s) and " + c.raws_copied +
        " RAW(s) to " + data.report.destination +
        (c.failures ? "\n" + c.failures + " failure(s) - see export-report.json" : "") +
        "\nOriginals untouched.", 7000);
    }).catch(function (err) {
      el.exportGo.disabled = false;
      el.exportGo.textContent = "Export";
      el.modalError.textContent = err.message;
      el.modalError.classList.remove("hidden");
    });
  }

  el.exportGo.addEventListener("click", runExport);
  el.exportCancel.addEventListener("click", closeExport);
  el.dest.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); runExport(); }
    ev.stopPropagation();
  });

  // ---- keyboard -----------------------------------------------------------

  document.addEventListener("keydown", function (ev) {
    if (!el.modal.classList.contains("hidden")) {
      if (ev.key === "Escape") { ev.preventDefault(); closeExport(); }
      return;
    }
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;

    // Shift+arrow browses without touching the decision. Checked before the
    // switch so Shift+Left does not fall through to "reject".
    if (ev.shiftKey) {
      if (ev.key === "ArrowLeft") { ev.preventDefault(); back(); return; }
      if (ev.key === "ArrowRight") { ev.preventDefault(); forward(); return; }
    }

    switch (ev.key) {
      case "ArrowLeft":  ev.preventDefault(); decide("reject"); break;
      case "ArrowRight": ev.preventDefault(); decide("keep"); break;
      case "ArrowUp":    ev.preventDefault(); decide("maybe"); break;
      case "ArrowDown":  ev.preventDefault(); undo(); break;
      case " ":          ev.preventDefault(); skip(); break;
      case "Escape":     ev.preventDefault(); closeZoom(); break;
      default:
        var k = ev.key.toLowerCase();
        if (k === "u") { ev.preventDefault(); undo(); }
        else if (k === "z") { ev.preventDefault(); if (!ev.repeat) openZoom(); }
        else if (k === "e") { ev.preventDefault(); openExport(); }
        // "," / "." unshifted, "<" / ">" when shift is held.
        else if (k === "," || k === "<") { ev.preventDefault(); back(); }
        else if (k === "." || k === ">") { ev.preventDefault(); forward(); }
    }
  });

  document.addEventListener("keyup", function (ev) {
    if (ev.key && ev.key.toLowerCase() === "z") closeZoom();
  });

  window.addEventListener("blur", closeZoom);

  // ---- swipe aliases ------------------------------------------------------

  var touch = null;
  el.photo.addEventListener("touchstart", function (ev) {
    if (ev.touches.length !== 1) return;
    touch = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
  }, { passive: true });

  el.photo.addEventListener("touchend", function (ev) {
    if (!touch) return;
    var t = ev.changedTouches[0];
    var dx = t.clientX - touch.x;
    var dy = t.clientY - touch.y;
    touch = null;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
      decide(dx < 0 ? "reject" : "keep");
    }
  }, { passive: true });

  // Trackpad two-finger swipe arrives as horizontal wheel deltas.
  var wheelAccum = 0;
  var wheelReset = null;
  var wheelCooldown = 0;
  window.addEventListener("wheel", function (ev) {
    if (zoomOpen || !el.modal.classList.contains("hidden")) return;
    if (Math.abs(ev.deltaX) <= Math.abs(ev.deltaY)) return;
    var now = Date.now();
    if (now < wheelCooldown) return;
    wheelAccum += ev.deltaX;
    if (wheelReset) clearTimeout(wheelReset);
    wheelReset = setTimeout(function () { wheelAccum = 0; }, 300);
    if (Math.abs(wheelAccum) > 120) {
      decide(wheelAccum > 0 ? "reject" : "keep");
      wheelAccum = 0;
      wheelCooldown = now + 320;
    }
  }, { passive: true });

  // ---- boot ---------------------------------------------------------------

  Promise.all([getJSON("/api/session"), getJSON("/api/photos")])
    .then(function (results) {
      var session = results[0];
      photos = results[1].photos;
      counts = session.counts;
      index = Math.max(0, Math.min(session.position, photos.length - 1));
      photos.forEach(function (p) { byId[p.id] = p; });
      document.title = "Photo Culler — " + session.root;
      render();
      if (session.complete) {
        toast("All " + photos.length + " decided - " + counts.keep + " keep, "
          + counts.maybe + " maybe, " + counts.reject + " reject. Reviewing from "
          + "the start; press E to export.", 6000);
      }
      if (session.unpaired_raws) {
        toast(session.unpaired_raws + " unpaired RAW file(s) found. They are recorded "
          + "in the export report but not shown for review.", 5000);
      }
    })
    .catch(function (err) {
      toast("Could not load the session: " + err.message, 10000);
    });
})();
