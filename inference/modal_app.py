import os
import modal

# 1. Define App Name
app = modal.App("tashkent-uhi-inference")

# 2. Resolve Monorepo Root Directory Path
INFERENCE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(INFERENCE_DIR)

# File Paths Relative to Monorepo Root
MODEL_WEIGHTS_PATH = os.path.join(INFERENCE_DIR, "dlstfm_traced_model.pt")
PIPELINE_SCRIPT_PATH = os.path.join(INFERENCE_DIR, "prep.py")  # or run_daily_lst_job.py
CITY_GEOJSON_PATH = os.path.join(ROOT_DIR, "tashkent_city.geojson")
ADMIN_GEOJSON_PATH = os.path.join(ROOT_DIR, "public", "data", "tashkent_city_renderable.geojson")


# 3. Build Modal Container Image & Mount Required Assets
image = (
    modal.Image.debian_slim()
    .pip_install(
        "torch",
        "numpy",
        "rasterio",
        "pyproj",
        "earthengine-api",
        "boto3",
        "requests",
        "tqdm",
        "matplotlib",
        "shapely",
        "pillow",
    )
    # Mount traced PyTorch weights into container working directory
    .add_local_file(MODEL_WEIGHTS_PATH, remote_path="/root/dlstfm_traced_model.pt")
    # Mount boundary & administrative GeoJSONs
    .add_local_file(CITY_GEOJSON_PATH, remote_path="/root/tashkent_city.geojson")
    .add_local_file(ADMIN_GEOJSON_PATH, remote_path="/root/tashkent_city_renderable.geojson")
    # Mount core pipeline execution script
    .add_local_file(PIPELINE_SCRIPT_PATH, remote_path="/root/prep.py")
)


# 4. Scheduled Daily Inference Task
@app.function(
    image=image,
    gpu="T4",  # Uses NVIDIA T4 GPU (100% free within Modal's $30/mo credits)
    secrets=[modal.Secret.from_name("uhi-pipeline-secrets")],
    timeout=600,
    schedule=modal.Cron("0 19 * * *")  # Triggers daily at 19:00 UTC (00:00 Midnight Tashkent time)
)
def daily_job():
    import sys
    sys.path.append("/root")
    from prep import run_pipeline  # Main execution entrypoint

    print("🚀 Triggering Scheduled Tashkent Daily LST Pipeline on Modal GPU...")
    run_pipeline(
        geojson_path="/root/tashkent_city.geojson",
        t0_date=None,  # Automatically calculates 16 days prior
        tk_date=None,  # Automatically picks today's date
        batch_size=16
    )


# 5. Local Test CLI Entrypoint
@app.local_entrypoint()
def main():
    print("🧪 Triggering manual test run on Modal GPU...")
    daily_job.remote()