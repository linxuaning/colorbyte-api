#!/usr/bin/env python3
"""
Example Usage: Replicate Free Tier Photo Restoration

This file demonstrates how to use the updated Replicate integration
in your own code.
"""

import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import the AI service
from app.services.ai_service import get_ai_service, ReplicateProvider


# Example 1: Basic usage with the service factory
async def example_basic():
    """Basic usage through the service factory."""
    print("=" * 60)
    print("Example 1: Basic Usage")
    print("=" * 60)

    # Get the configured AI service (automatically uses provider from .env)
    ai_service = get_ai_service()

    # Process a photo
    result = await ai_service.process_photo(
        input_path="uploads/test_photo.jpg",
        output_path="results/example1_output.jpg",
        colorize=False,  # Colorization not available in free tier
        progress_callback=None  # Optional: add callback for progress
    )

    if result.success:
        print(f"✓ Success! Output: {result.output_path}")
    else:
        print(f"✗ Failed: {result.error}")


# Example 2: With progress tracking
async def example_with_progress():
    """Usage with progress tracking."""
    print("\n" + "=" * 60)
    print("Example 2: With Progress Tracking")
    print("=" * 60)

    # Define a progress callback
    async def show_progress(message: str, progress: int):
        bar_length = 40
        filled = int(bar_length * progress / 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        print(f"\r[{bar}] {progress:3d}% - {message}", end="", flush=True)

    ai_service = get_ai_service()

    result = await ai_service.process_photo(
        input_path="uploads/test_photo.jpg",
        output_path="results/example2_output.jpg",
        colorize=False,
        progress_callback=show_progress
    )

    print()  # New line after progress bar

    if result.success:
        print(f"✓ Success! Output: {result.output_path}")
    else:
        print(f"✗ Failed: {result.error}")


# Example 3: Direct provider usage
async def example_direct_provider():
    """Direct usage of ReplicateProvider."""
    print("\n" + "=" * 60)
    print("Example 3: Direct Provider Usage")
    print("=" * 60)

    api_token = os.getenv("REPLICATE_API_TOKEN")
    if not api_token:
        print("✗ REPLICATE_API_TOKEN not set")
        return

    # Create provider directly
    provider = ReplicateProvider(api_token)

    async def log_progress(msg: str, pct: int):
        print(f"[{pct:3d}%] {msg}")

    result = await provider.process_photo(
        input_path="uploads/test_photo.jpg",
        output_path="results/example3_output.jpg",
        colorize=False,
        progress_callback=log_progress
    )

    if result.success:
        print(f"✓ Success! Output: {result.output_path}")
    else:
        print(f"✗ Failed: {result.error}")


# Example 4: Batch processing
async def example_batch_processing():
    """Process multiple images."""
    print("\n" + "=" * 60)
    print("Example 4: Batch Processing")
    print("=" * 60)

    ai_service = get_ai_service()
    uploads_dir = Path("uploads")
    results_dir = Path("results/batch")
    results_dir.mkdir(exist_ok=True, parents=True)

    # Find all images
    image_files = list(uploads_dir.glob("*.jpg")) + list(uploads_dir.glob("*.png"))
    print(f"Found {len(image_files)} images to process")

    for i, image_path in enumerate(image_files[:3], 1):  # Process first 3
        print(f"\nProcessing {i}/{min(3, len(image_files))}: {image_path.name}")

        output_path = results_dir / f"restored_{image_path.name}"

        result = await ai_service.process_photo(
            input_path=str(image_path),
            output_path=str(output_path),
            colorize=False
        )

        if result.success:
            print(f"  ✓ Saved to {output_path}")
        else:
            print(f"  ✗ Failed: {result.error}")


# Example 5: Error handling
async def example_error_handling():
    """Proper error handling."""
    print("\n" + "=" * 60)
    print("Example 5: Error Handling")
    print("=" * 60)

    ai_service = get_ai_service()

    try:
        result = await ai_service.process_photo(
            input_path="uploads/test_photo.jpg",
            output_path="results/example5_output.jpg",
            colorize=False
        )

        if result.success:
            print(f"✓ Success! Output: {result.output_path}")

            # Verify output file
            output = Path(result.output_path)
            if output.exists():
                size_mb = output.stat().st_size / (1024 * 1024)
                print(f"  File size: {size_mb:.2f} MB")
            else:
                print("  Warning: Output file not found!")
        else:
            print(f"✗ Processing failed: {result.error}")

            # Handle specific errors
            if "credits exhausted" in result.error.lower():
                print("  → You've exceeded free tier limits")
                print("  → Add billing at https://replicate.com/account/billing")
            elif "rate limit" in result.error.lower():
                print("  → Too many requests, wait a few minutes")
            elif "all restoration methods failed" in result.error.lower():
                print("  → Try a different image or check Replicate status")
            else:
                print("  → Check logs for details")

    except Exception as e:
        print(f"✗ Unexpected error: {e}")


# Example 6: Model-specific behavior
async def example_model_info():
    """Information about which models are used."""
    print("\n" + "=" * 60)
    print("Example 6: Model Information")
    print("=" * 60)

    print("\nFree Tier Models Used:")
    print("1. GFPGAN (Primary)")
    print("   - Model: tencentarc/gfpgan")
    print("   - Best for: Old photos with faces")
    print("   - Parameters: version=v1.4, scale=2")
    print()
    print("2. CodeFormer (Fallback)")
    print("   - Model: sczhou/codeformer")
    print("   - Best for: Face restoration quality control")
    print("   - Parameters: upscale=2, fidelity=0.7")
    print()
    print("3. Real-ESRGAN (Last Resort)")
    print("   - Model: nightmareai/real-esrgan")
    print("   - Best for: Upscaling when face enhancement fails")
    print("   - Parameters: scale=2, face_enhance=True")

    print("\nFallback Strategy:")
    print("  GFPGAN → CodeFormer → Real-ESRGAN")
    print("  (Each tried in sequence if previous fails)")


# Example 7: Integration with FastAPI
def example_fastapi_integration():
    """Show how it integrates with FastAPI endpoints."""
    print("\n" + "=" * 60)
    print("Example 7: FastAPI Integration")
    print("=" * 60)

    example_code = '''
from fastapi import FastAPI, UploadFile
from app.services.ai_service import get_ai_service

app = FastAPI()

@app.post("/restore")
async def restore_photo(file: UploadFile):
    """Restore an uploaded photo."""

    # Save uploaded file
    input_path = f"uploads/{file.filename}"
    with open(input_path, "wb") as f:
        f.write(await file.read())

    # Process with AI service
    ai_service = get_ai_service()
    result = await ai_service.process_photo(
        input_path=input_path,
        output_path=f"results/restored_{file.filename}",
        colorize=False
    )

    if result.success:
        return {"status": "success", "output": result.output_path}
    else:
        return {"status": "error", "message": result.error}
'''
    print(example_code)


# Main function
async def main():
    """Run all examples."""
    print("\n" + "🎨" * 30)
    print("Replicate Free Tier - Usage Examples")
    print("🎨" * 30)

    # Check if API token is set
    api_token = os.getenv("REPLICATE_API_TOKEN")
    if not api_token or api_token == "your_replicate_api_token_here":
        print("\n⚠️  WARNING: REPLICATE_API_TOKEN not set!")
        print("Set it in .env file before running examples")
        print("\nShowing code examples only (no actual processing):\n")

        example_model_info()
        example_fastapi_integration()
        return

    print("\n✓ REPLICATE_API_TOKEN is set")
    print("Running live examples with actual API calls...\n")

    # Check for test images
    test_image = "uploads/49aec47cbde44ddf8acb76d0fdf8cd4c.jpg"
    if not Path(test_image).exists():
        print(f"\n⚠️  Test image not found: {test_image}")
        print("Place a test image at that path to run examples")
        return

    # Run examples
    try:
        await example_basic()
        # await example_with_progress()
        # await example_direct_provider()
        # await example_batch_processing()
        # await example_error_handling()
        example_model_info()
        example_fastapi_integration()

        print("\n" + "=" * 60)
        print("Examples completed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ Error running examples: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
