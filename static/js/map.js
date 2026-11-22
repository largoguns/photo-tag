/* Maps (spec section 5.15), loaded only where needed, after Leaflet:
 * - [data-location-picker]: an administrator places a location (OSM search or a tap).
 * - [data-photo-map]: the placed locations with photo counts and thumbnails.
 * Tiles and searches go straight from the browser to OpenStreetMap, sending only the
 * origin as referrer (the site's own policy is "same-origin").
 */
(function () {
  "use strict";
  if (!window.L) return;

  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  const NOMINATIM = "https://nominatim.openstreetmap.org/search";
  const REFERRER = "strict-origin-when-cross-origin";
  const SPAIN = [40.2, -3.7];

  function baseMap(element, options) {
    const map = L.map(element, { zoomControl: true, ...options });
    L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION, referrerPolicy: REFERRER }).addTo(map);
    // The element may get its final size after the first layout (grid, fonts).
    window.setTimeout(() => map.invalidateSize(), 0);
    window.addEventListener("resize", () => map.invalidateSize());
    return map;
  }

  // --- Placing a location (administration) ---------------------------------------------
  function initPicker(element) {
    const form = document.querySelector("[data-place-form]");
    const latField = form && form.querySelector("[data-place-lat]");
    const lngField = form && form.querySelector("[data-place-lng]");
    const locality = form && form.querySelector("[data-place-locality]");
    // Tolerate a decimal comma ("37,38"): parseFloat would silently read it as 37.
    const number = (text) => parseFloat(String(text || "").replace(",", "."));
    const lat = number(element.dataset.lat);
    const lng = number(element.dataset.lng);
    const placed = !Number.isNaN(lat) && !Number.isNaN(lng);
    const map = baseMap(element, {});
    map.setView(placed ? [lat, lng] : SPAIN, placed ? 16 : 5);
    let marker = null;

    const status = document.querySelector("[data-place-status]");
    const save = form && form.querySelector("[data-place-save]");

    // Moved but not saved yet: say so and highlight the one button that saves the point.
    function markUnsaved() {
      if (status && status.dataset.unsaved) {
        status.textContent = status.dataset.unsaved;
        status.classList.add("place-unsaved");
      }
      if (save) save.classList.add("pulse");
    }

    function setPoint(latlng, zoom, initial) {
      if (!marker) {
        marker = L.marker(latlng, { draggable: true, autoPan: true, keyboard: true }).addTo(map);
        marker.on("dragend", () => setPoint(marker.getLatLng()));
      } else {
        marker.setLatLng(latlng);
      }
      if (zoom) map.setView(latlng, zoom);
      if (latField) latField.value = latlng.lat.toFixed(6);
      if (lngField) lngField.value = latlng.lng.toFixed(6);
      if (!initial) markUnsaved();
    }
    if (placed) setPoint(L.latLng(lat, lng), null, true);
    map.on("click", (event) => setPoint(event.latlng));

    const search = document.querySelector("[data-geo-search]");
    if (!search) return;
    const results = search.querySelector("[data-geo-results]");
    const message = search.querySelector("[data-geo-message]");
    let lastSearch = 0;

    function say(text) {
      message.textContent = text || "";
      message.hidden = !text;
    }

    function townOf(address) {
      return address ? address.city || address.town || address.village || address.municipality || "" : "";
    }

    search.addEventListener("submit", async (event) => {
      event.preventDefault();
      const query = search.querySelector("input[type=search]").value.trim();
      if (!query) return;
      // Nominatim's usage policy: at most one request per second, never on every key.
      const wait = 1100 - (Date.now() - lastSearch);
      if (wait > 0) await new Promise((resolve) => window.setTimeout(resolve, wait));
      lastSearch = Date.now();
      const params = new URLSearchParams({
        q: query, format: "jsonv2", addressdetails: "1", limit: "5",
        "accept-language": document.documentElement.lang || "es",
      });
      results.innerHTML = "";
      results.hidden = true;
      say("");
      let found = [];
      try {
        const response = await fetch(`${NOMINATIM}?${params}`, { referrerPolicy: REFERRER, headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(String(response.status));
        found = await response.json();
      } catch (error) {
        say(element.dataset.searchError);
        return;
      }
      if (!found.length) { say(element.dataset.noResults); return; }
      found.forEach((place) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "geo-result";
        button.textContent = place.display_name;
        button.addEventListener("click", () => {
          const town = place.addresstype === "city" || place.addresstype === "town" || place.addresstype === "village";
          setPoint(L.latLng(parseFloat(place.lat), parseFloat(place.lon)), town ? 14 : 17);
          if (locality && !locality.value.trim()) locality.value = townOf(place.address);
          results.hidden = true;
          element.scrollIntoView({ block: "nearest", behavior: "smooth" });
        });
        item.appendChild(button);
        results.appendChild(item);
      });
      results.hidden = false;
    });
  }

  // --- Photos on the map -------------------------------------------------------------------
  function countLabel(element, count) {
    return count === 1 ? element.dataset.photoLabel : element.dataset.photosLabel.replace("%s", count);
  }

  function initPhotoMap(element) {
    const data = document.getElementById("map-places");
    let places = [];
    try { places = JSON.parse(data.textContent); } catch (error) { places = []; }
    const map = baseMap(element, { worldCopyJump: true });
    const detail = document.querySelector("[data-map-detail]");

    // Clusters add up photos, not places: "37" means 37 photos around there.
    const cluster = L.markerClusterGroup({
      showCoverageOnHover: false,
      maxClusterRadius: 70,
      spiderfyOnMaxZoom: true,
      iconCreateFunction(group) {
        const total = group.getAllChildMarkers().reduce((sum, m) => sum + m.options.photoCount, 0);
        const size = total >= 100 ? 64 : total >= 10 ? 54 : 46;
        return L.divIcon({
          html: `<span class="map-cluster" style="width:${size}px;height:${size}px">${total}</span>`,
          className: "map-cluster-icon",
          iconSize: [size, size],
        });
      },
    });

    function showDetail(place) {
      if (!detail) return;
      detail.querySelector("[data-map-name]").textContent = place.name;
      const locality = detail.querySelector("[data-map-locality]");
      locality.textContent = place.locality || "";
      locality.hidden = !place.locality;
      const thumbs = detail.querySelector("[data-map-thumbs]");
      thumbs.innerHTML = "";
      place.photos.forEach((photo) => {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = photo.url;
        const img = document.createElement("img");
        img.src = photo.thumb; img.alt = ""; img.loading = "lazy";
        link.appendChild(img);
        item.appendChild(link);
        thumbs.appendChild(item);
      });
      const gallery = detail.querySelector("[data-map-gallery]");
      gallery.href = place.gallery;
      detail.querySelector("[data-map-count]").textContent = `(${countLabel(element, place.count)})`;
      detail.hidden = false;
      document.body.classList.add("map-detail-open");
      window.setTimeout(() => map.invalidateSize(), 0);
    }

    function hideDetail() {
      if (!detail || detail.hidden) return;
      detail.hidden = true;
      document.body.classList.remove("map-detail-open");
      window.setTimeout(() => map.invalidateSize(), 0);
    }

    places.forEach((place) => {
      const thumb = place.photos.length ? place.photos[0].thumb : "";
      const html = `<span class="map-marker">${thumb ? `<img src="${thumb}" alt="">` : ""}<span class="map-count">${place.count}</span></span>`;
      const marker = L.marker([place.lat, place.lng], {
        icon: L.divIcon({ html, className: "map-marker-icon", iconSize: [56, 56], iconAnchor: [28, 28] }),
        title: `${place.name} · ${countLabel(element, place.count)}`,
        keyboard: true,
        photoCount: place.count,
      });
      marker.on("click", () => showDetail(place));
      cluster.addLayer(marker);
    });
    map.addLayer(cluster);
    if (places.length === 1) map.setView([places[0].lat, places[0].lng], 15);
    else if (places.length) map.fitBounds(cluster.getBounds(), { padding: [40, 40], maxZoom: 15 });
    else map.setView(SPAIN, 5);

    map.on("click", hideDetail);
    const close = detail && detail.querySelector("[data-map-close]");
    if (close) close.addEventListener("click", hideDetail);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideDetail(); });
  }

  function init() {
    const picker = document.querySelector("[data-location-picker]");
    if (picker) initPicker(picker);
    const photoMap = document.querySelector("[data-photo-map]");
    if (photoMap && L.markerClusterGroup) initPhotoMap(photoMap);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
