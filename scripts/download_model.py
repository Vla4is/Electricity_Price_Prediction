"""Download MODEL_NAME into HF_HOME (models/), skipping files already present."""
import os
from dotenv import load_dotenv
load_dotenv()  # must run before importing huggingface_hub, which reads HF_HOME at import time
from huggingface_hub import snapshot_download
print(snapshot_download(os.getenv("MODEL_NAME", "amazon/chronos-bolt-small")))
