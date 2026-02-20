#!/usr/bin/env python3
"""
Test script for Replicate Free Tier Photo Restoration API integration.

This script tests the updated ReplicateProvider with free tier models:
- GFPGAN (tencentarc/gfpgan) - primary restoration method
- CodeFormer (sczhou/codeformer) - fallback restoration method
- Real-ESRGAN (nightmareai/real-esrgan) - upscaling fallback

Usage:
    python test_replicate_free.py <input_image_path>

Requirements:
    - REPLICATE_API_TOKEN environment variable set (or in .env file)
    - Input image file
"""

import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Add app directory to path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.ai_service import ReplicateProvider, ProcessingResult


async def progress_callback(message: str, progress: int):
    """Display progress updates."""
    print(f"[{progress:3d}%] {message}")


async def test_replicate_free():
    """Test the Replicate free tier integration."""

    # Check if REPLICATE_API_TOKEN is set
    api_token = os.getenv("REPLICATE_API_TOKEN")
    if not api_token:
        print("Error: REPLICATE_API_TOKEN environment variable not set!")
        print("Please set it in your .env file or export it:")
        print("  export REPLICATE_API_TOKEN='your_token_here'")
        sys.exit(1)

    # Get input image path
    if len(sys.argv) < 2:
        print("Usage: python test_replicate_free.py <input_image_path>")
        print("\nExample:")
        print("  python test_replicate_free.py uploads/test_photo.jpg")
        sys.exit(1)

    input_path = sys.argv[1]
    if not Path(input_path).exists():
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)

    # Set up output path
    output_path = Path("results") / f"test_output_{Path(input_path).name}"
    output_path.parent.mkdir(exist_ok=True)

    print("=" * 60)
    print("Testing Replicate Free Tier Photo Restoration")
    print("=" * 60)
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print("=" * 60)
    print()

    # Create provider and test
    provider = ReplicateProvider(api_token)

    print("Starting photo restoration process...")
    print("This will use the fallback strategy:")
    print("  1. Try GFPGAN (best for old photos)")
    print("  2. If fails, try CodeFormer")
    print("  3. If both fail, use Real-ESRGAN")
    print()

    result: ProcessingResult = await provider.process_photo(
        input_path=str(input_path),
        output_path=str(output_path),
        colorize=False,  # Colorization not available in free tier
        progress_callback=progress_callback
    )

    print()
    print("=" * 60)

    if result.success:
        print("SUCCESS! Photo restoration completed.")
        print(f"Output saved to: {result.output_path}")
        print()
        print("Check the output file to verify the restoration quality.")
    else:
        print("FAILED! Photo restoration encountered an error.")
        print(f"Error: {result.error}")

    print("=" * 60)

    return result.success


if __name__ == "__main__":
    success = asyncio.run(test_replicate_free())
    sys.exit(0 if success else 1)
