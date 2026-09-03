"""
Launcher script for Indy7 3D Digital Twin Workcell.
Usage:
    uv run python run_digitaltwin.py
"""

import uvicorn
from src.digitaltwin.config import SERVER_HOST, SERVER_PORT


def main():
    print("=" * 65)
    print("      NEUROMEKA INDY7 3D DIGITAL TWIN & PALLETIZING WORKCELL")
    print(f"      Dashboard running at: http://localhost:{SERVER_PORT}")
    print("=" * 65)

    uvicorn.run(
        "src.digitaltwin.server:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
