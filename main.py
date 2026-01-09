from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse
import requests, json, os, io, csv
from shapely.geometry import LineString, mapping
from shapely.ops import transform
import pyproj
import osmnx as ox

app = FastAPI()

CACHE_FILE = "town_state_cache.json"
LATEST_RESULT = {}

FUN_PIANOS_YELLOW = "#fbba30"

# ---------------- Cache helpers ----------------

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

town_state_cache = load_cache()

# ---------------- Helpers ----------------

def geocode(address: str):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "tour-route-tool"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    data = r.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])

def get_route(start_lat, start_lon, end_lat, end_lon):
    url = f"https://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    params = {"overview": "full", "geometries": "geojson"}
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    return data["routes"][0]["geometry"]["coordinates"]

def miles_to_meters(miles):
    return miles * 1609.34

def sample_route_points_even(route_coords, max_points=6):
    n = len(route_coords)
    if n <= max_points:
        return [(lat, lon) for lon, lat in route_coords]
    idxs = [round(i * (n - 1) / (max_points - 1)) for i in range(max_points)]
    return [(route_coords[i][1], route_coords[i][0]) for i in idxs]

def towns_near_points(points, radius_meters=15000):
    tags = {"place": ["city", "town", "village"]}
    found = {}

    for lat, lon in points:
        try:
            gdf = ox.features_from_point((lat, lon), tags=tags, dist=radius_meters)
            for _, row in gdf.iterrows():
                name = row.get("name")
                geom = row.get("geometry")
                if not name or geom is None:
                    continue
                if name not in found:
                    p = geom.representative_point()
                    found[name] = (p.y, p.x)
        except Exception:
            continue

    results = []
    for name, (tlat, tlon) in sorted(found.items()):
        if name in town_state_cache:
            results.append((name, town_state_cache[name]))
            continue

        try:
            url = "https://nominatim.openstreetmap.org/reverse"
            params = {"lat": tlat, "lon": tlon, "format": "json", "zoom": 10}
            headers = {"User-Agent": "tour-route-tool"}
            r = requests.get(url, params=params, headers=headers, timeout=15)
            data = r.json()
            addr = data.get("address", {})
            state = addr.get("state_code") or addr.get("state")
            town_state_cache[name] = state or ""
            results.append((name, state or ""))
        except Exception:
            results.append((name, ""))

    save_cache(town_state_cache)
    return results

# ---------------- CSV export ----------------

@app.get("/export_csv")
def export_csv():
    if not LATEST_RESULT:
        return {"error": "No data to export"}

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([
        "tour_name",
        "start_location",
        "end_location",
        "radius_miles",
        "town",
        "state"
    ])

    for town, state in LATEST_RESULT["towns"]:
        writer.writerow([
            LATEST_RESULT["tour_name"],
            LATEST_RESULT["start_location"],
            LATEST_RESULT["end_location"],
            LATEST_RESULT["radius"],
            town,
            state
        ])

    buffer.seek(0)
    filename = f"{LATEST_RESULT['tour_name'].replace(' ', '_')}_towns.csv"

    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ---------------- Routes ----------------

@app.get("/", response_class=HTMLResponse)
def home():
    return f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background:#fafafa; padding:30px; }}
            .card {{
                background:#fff; padding:25px; max-width:500px; margin:auto;
                border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,.1);
            }}
            button {{
                background:{FUN_PIANOS_YELLOW};
                border:none; padding:10px 18px; border-radius:5px;
                cursor:pointer; font-weight:bold;
            }}
            button:hover {{ filter:brightness(0.9); }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Fun Pianos Tour Route Tool</h1>
            <form method="post" action="/loading">
                <label>Tour Name</label><br>
                <input type="text" name="tour_name" required><br><br>

                <label>Radius (miles)</label><br>
                <input type="number" name="radius" value="1" max="4"><br><br>

                <label>Start Location</label><br>
                <input type="text" name="start_location" required><br><br>

                <label>End Location</label><br>
                <input type="text" name="end_location" required><br><br>

                <button type="submit">Calculate</button>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/loading", response_class=HTMLResponse)
def loading(
    tour_name: str = Form(...),
    radius: int = Form(...),
    start_location: str = Form(...),
    end_location: str = Form(...)
):
    return f"""
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #fafafa;
                text-align: center;
                padding-top: 80px;
            }}
            h2 {{
                color: #333;
            }}
        </style>
    </head>
    <body>
        <h2>Calculating route…</h2>

        <script>
            setTimeout(() => {{
                window.location.href =
                    "/calculate"
                    + "?tour_name={tour_name}"
                    + "&radius={radius}"
                    + "&start_location={start_location}"
                    + "&end_location={end_location}";
            }}, 600);
        </script>
    </body>
    </html>
    """


@app.get("/calculate", response_class=HTMLResponse)
def calculate(tour_name: str, radius: int, start_location: str, end_location: str):
    start = geocode(start_location)
    end = geocode(end_location)

    route = get_route(start[0], start[1], end[0], end[1])
    route_latlng = [[lat, lon] for lon, lat in route]

    line = LineString(route)
    to_m = pyproj.Transformer.from_crs("EPSG:4326","EPSG:3857",always_xy=True).transform
    to_ll = pyproj.Transformer.from_crs("EPSG:3857","EPSG:4326",always_xy=True).transform
    buffer = transform(to_ll, transform(to_m, line).buffer(miles_to_meters(radius)))
    buffer_geojson = json.dumps(mapping(buffer))

    towns = towns_near_points(sample_route_points_even(route))

    LATEST_RESULT.clear()
    LATEST_RESULT.update({
        "tour_name": tour_name,
        "start_location": start_location,
        "end_location": end_location,
        "radius": radius,
        "towns": towns
    })

    town_list_html = "".join(
        f"<li>{t}{', ' + s if s else ''}</li>" for t, s in towns
    )

    return f"""
    <html>
    <head>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <style>
            body {{ font-family:Arial, sans-serif; background:#fafafa; padding:20px; }}
            header {{ display:flex; align-items:center; gap:15px; }}
            header img {{ height:48px; }}
            .summary {{
                background:#fff; padding:15px; margin:15px 0;
                border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.1);
            }}
            .button {{
                background:{FUN_PIANOS_YELLOW};
                padding:10px 16px; border-radius:5px;
                text-decoration:none; color:#000; font-weight:bold;
                display:inline-block; margin-right:10px;
            }}
            .button:hover {{ filter:brightness(0.9); }}
            #container {{ display:flex; gap:20px; }}
            #map {{ height:420px; width:62%; border:1px solid #ccc; border-radius:6px; }}
            #sidebar {{ width:38%; background:#fff; padding:15px;
                        border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.1); }}
            ul {{ list-style:none; padding:0; }}
            li {{ padding:6px 0; border-bottom:1px solid #eee; }}
        </style>
    </head>
    <body>

        <header>
            <img src="https://funpianos.com/favicon.ico">
            <h1>Fun Pianos Tour Route Results</h1>
        </header>

        <div class="summary">
            <strong>Tour:</strong> {tour_name}<br>
            <strong>From:</strong> {start_location}<br>
            <strong>To:</strong> {end_location}<br>
            <strong>Radius:</strong> {radius} miles<br>
            <strong>Towns found:</strong> {len(towns)}
        </div>

        <a class="button" href="/">← Back to Form</a>
        <a class="button" href="/export_csv">Download CSV</a>

        <div id="container">
            <div id="map"></div>
            <div id="sidebar">
                <h3>Towns on the way</h3>
                <ul>{town_list_html}</ul>
            </div>
        </div>

        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script>
            var map = L.map('map').setView([{start[0]}, {start[1]}], 7);
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png').addTo(map);
            L.polyline({route_latlng}, {{ color:'blue' }}).addTo(map);
            L.geoJSON({buffer_geojson}, {{ style:{{color:'green', fillOpacity:0.25}} }}).addTo(map);
        </script>

    </body>
    </html>
    """
