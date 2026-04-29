#!/usr/bin/env python3
"""
Pre-fetch Smart Turn v3 model weights.

The Smart Turn v3 model is bundled with Pipecat, but this script ensures
the ONNX runtime is properly initialized and the model is loaded and cached
before the first call comes in.

Usage:
    python scripts/prefetch_smart_turn_model.py

This can be run during container startup or as part of worker initialization.
"""

import sys
import time


def prefetch_model():
    """Pre-fetch and warm up the Smart Turn v3 model."""
    print("Pre-fetching Smart Turn v3 model...")
    start = time.monotonic()

    try:
        # Import the model - this triggers ONNX model loading
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )

        # Create an instance to ensure model weights are loaded
        analyzer = LocalSmartTurnAnalyzerV3()

        elapsed = (time.monotonic() - start) * 1000
        print(f"Smart Turn v3 model loaded successfully in {elapsed:.1f}ms")

        # Print model info if available
        if hasattr(analyzer, "_model") and analyzer._model is not None:
            print("Model initialized and ready for inference")

        return True

    except ImportError as e:
        print(f"Error: Failed to import Smart Turn v3 module: {e}")
        print("Make sure pipecat-ai[local-smart-turn-v3] is installed")
        return False

    except Exception as e:
        print(f"Error loading Smart Turn v3 model: {e}")
        return False


def check_dependencies():
    """Check that required dependencies are installed."""
    print("Checking dependencies...")

    try:
        import onnxruntime

        print(f"  onnxruntime: {onnxruntime.__version__}")
    except ImportError:
        print("  onnxruntime: NOT INSTALLED")
        print("  Install with: pip install onnxruntime")
        return False

    try:
        import pipecat

        print(f"  pipecat-ai: {getattr(pipecat, '__version__', 'unknown')}")
    except ImportError:
        print("  pipecat-ai: NOT INSTALLED")
        return False

    return True


if __name__ == "__main__":
    print("=" * 50)
    print("Smart Turn v3 Model Pre-fetch")
    print("=" * 50)
    print()

    if not check_dependencies():
        print("\nDependency check failed. Please install missing packages.")
        sys.exit(1)

    print()
    success = prefetch_model()

    print()
    if success:
        print("Model pre-fetch completed successfully!")
        sys.exit(0)
    else:
        print("Model pre-fetch failed!")
        sys.exit(1)
