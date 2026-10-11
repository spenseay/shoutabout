/* ShoutAbout website: the only script. No trackers, no cookies, no storage, no requests
   except /downloads.json on this same site. Without it every page still reads fine: the
   download buttons stay on "Coming soon" and no contact email is shown. */
(function () {
  "use strict";

  /* ---------------------------------------------------- site configuration */
  /* /downloads.json holds the go-live switches: windows.live, mac.live, iphone.live
     (a button only links to a file once its flag is true) and contact_email (empty =
     no email anywhere on the site). */

  function formatSize(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes) || bytes <= 0) return "";
    return "about " + Math.round(bytes / 1000000) + " MB";
  }

  function lookup(config, path) {
    return path.split(".").reduce(function (obj, key) {
      return obj && obj[key] !== undefined ? obj[key] : undefined;
    }, config);
  }

  function isHttps(url) {
    return typeof url === "string" && /^https:\/\/[^\s"'<>]+$/.test(url);
  }

  // The link a platform's buttons use, or "" while its flag is off.
  function liveLink(config, platform) {
    var p = config[platform] || {};
    if (p.live !== true) return "";
    var url = platform === "iphone" ? p.app_store_url || p.testflight_url : p.url;
    return isHttps(url) ? url : "";
  }

  function readyLabel(el, config, platform) {
    if (platform === "iphone") {
      // The App Store link wins over TestFlight once both exist.
      return isHttps((config.iphone || {}).app_store_url)
        ? el.getAttribute("data-label-store")
        : el.getAttribute("data-label-testflight");
    }
    return el.getAttribute("data-label-ready");
  }

  function applyDownloads(config) {
    document.querySelectorAll("[data-dl]").forEach(function (button) {
      var platform = button.getAttribute("data-dl");
      var url = liveLink(config, platform);
      var labelEl = button.querySelector("[data-dl-label]") || button;
      if (!url) return; // the HTML already shows the "Coming soon" state
      var label = readyLabel(button, config, platform);
      if (label) labelEl.textContent = label;
      button.setAttribute("href", url);
      button.classList.remove("btn--soon");
      button.addEventListener("click", function () {
        document.querySelectorAll('[data-dl-help="' + platform + '"]').forEach(function (el) {
          el.hidden = false;
        });
      });
    });

    document.querySelectorAll("[data-dl-status]").forEach(function (el) {
      var platform = el.getAttribute("data-dl-status");
      if (!liveLink(config, platform)) return;
      var label = readyLabel(el, config, platform);
      if (label) el.textContent = label;
      el.classList.remove("chip--outline");
    });

    document.querySelectorAll("[data-dl-live]").forEach(function (el) {
      el.hidden = !liveLink(config, el.getAttribute("data-dl-live"));
    });
    document.querySelectorAll("[data-dl-soon]").forEach(function (el) {
      el.hidden = !!liveLink(config, el.getAttribute("data-dl-soon"));
    });

    // A size replaces the estimate in the page; a hidden size slot gets its own separator.
    // TestFlight-only notes go away once the App Store link is live.
    var onStore = !!liveLink(config, "iphone") && isHttps((config.iphone || {}).app_store_url);
    document.querySelectorAll("[data-dl-beta]").forEach(function (el) {
      el.hidden = onStore;
    });

    document.querySelectorAll("[data-dl-size]").forEach(function (el) {
      var size = formatSize(lookup(config, el.getAttribute("data-dl-size") + ".size_bytes"));
      if (!size) return;
      el.textContent = el.hidden ? " · " + size : size;
      el.hidden = false;
    });

    document.querySelectorAll("[data-dl-releases]").forEach(function (el) {
      if (isHttps(config.releases_url)) el.setAttribute("href", config.releases_url);
    });
  }

  function applyContact(config) {
    var email = typeof config.contact_email === "string" ? config.contact_email.trim() : "";
    if (!/^[^\s@<>"'()]+@[^\s@<>"'()]+\.[a-z]{2,}$/i.test(email)) return;
    document.querySelectorAll("[data-contact-link]").forEach(function (a) {
      a.setAttribute("href", "mailto:" + email);
      a.textContent = email;
    });
    document.querySelectorAll("[data-contact]").forEach(function (el) {
      el.hidden = false;
    });
    document.querySelectorAll("[data-contact-none]").forEach(function (el) {
      el.hidden = true;
    });
  }

  fetch("/downloads.json", { cache: "no-cache", credentials: "omit" })
    .then(function (response) {
      if (!response.ok) throw new Error("downloads.json: " + response.status);
      return response.json();
    })
    .then(function (config) {
      applyDownloads(config);
      applyContact(config);
    })
    .catch(function () {
      /* Keep the static page: "Coming soon" buttons and no email. */
    });

  /* --------------------------------------- pause talking mouths off-screen */

  if ("IntersectionObserver" in window) {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        entry.target.classList.toggle("is-paused", !entry.isIntersecting);
      });
    });
    document.querySelectorAll(".mouth.is-chatting").forEach(function (mouth) {
      watcher.observe(mouth);
    });
  }
})();
