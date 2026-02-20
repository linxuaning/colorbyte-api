# Quick Start: Replicate Free Tier Setup

## 1. Get Your Replicate API Token

1. Visit https://replicate.com
2. Sign up for a free account
3. Go to https://replicate.com/account/api-tokens
4. Click "Create token"
5. Copy your token

## 2. Configure Your Environment

Edit your `.env` file:

```bash
# Set the provider to use Replicate
ai_provider=replicate

# Add your actual Replicate API token
REPLICATE_API_TOKEN=r8_your_actual_token_here
```

## 3. Test the Integration

Run the test script with a sample image:

```bash
cd backend
source .venv/bin/activate
python test_replicate_free.py uploads/49aec47cbde44ddf8acb76d0fdf8cd4c.jpg
```

## 4. Expected Behavior

### Success Case
```
============================================================
Testing Replicate Free Tier Photo Restoration
============================================================
Input:  uploads/test_photo.jpg
Output: results/test_output_test_photo.jpg
============================================================

Starting photo restoration process...
This will use the fallback strategy:
  1. Try GFPGAN (best for old photos)
  2. If fails, try CodeFormer
  3. If both fail, use Real-ESRGAN

[ 10%] Uploading image...
[ 20%] Enhancing faces (GFPGAN)...
[ 60%] Additional upscaling (Real-ESRGAN)...
[ 95%] Downloading result...
[100%] Complete

============================================================
SUCCESS! Photo restoration completed.
Output saved to: results/test_output_test_photo.jpg
============================================================
```

### Fallback Case (GFPGAN fails, CodeFormer succeeds)
```
[ 10%] Uploading image...
[ 20%] Enhancing faces (GFPGAN)...
WARNING: GFPGAN failed: [error message]
[ 25%] Enhancing faces (CodeFormer)...
[ 60%] Additional upscaling (Real-ESRGAN)...
[ 95%] Downloading result...
[100%] Complete
```

## 5. Integration with Your App

The free tier integration is drop-in compatible with the existing code:

```python
from app.services.ai_service import get_ai_service

# Get the service (automatically uses configured provider)
ai_service = get_ai_service()

# Process a photo
result = await ai_service.process_photo(
    input_path="uploads/photo.jpg",
    output_path="results/restored.jpg",
    colorize=False,  # Note: colorization not available in free tier
    progress_callback=None  # Optional callback for progress updates
)

if result.success:
    print(f"Success! Output: {result.output_path}")
else:
    print(f"Failed: {result.error}")
```

## 6. Model Behavior

### GFPGAN (Primary)
- Best for: Old photos with faces
- Processing time: ~20-40 seconds
- Output: Enhanced faces + 2x upscaling

### CodeFormer (Fallback)
- Best for: Face restoration with quality control
- Processing time: ~25-45 seconds
- Output: Enhanced faces + 2x upscaling

### Real-ESRGAN (Last Resort)
- Best for: Upscaling without face detection
- Processing time: ~15-30 seconds
- Output: 2x upscaling

## 7. Free Tier Limits

Replicate's free tier typically includes:
- **Limited API calls per month** (check your account)
- **Rate limiting** (handled automatically with retry)
- **Queue priority** (may be slower than paid tier)

Monitor your usage at: https://replicate.com/account

## 8. Troubleshooting

### "REPLICATE_API_TOKEN environment variable not set"
- Check your `.env` file
- Make sure `REPLICATE_API_TOKEN=r8_...` is set
- Restart your application

### "Replicate credits exhausted (402)"
- You've exceeded free tier limits
- Wait for monthly reset
- Or add billing at https://replicate.com/account/billing

### "All restoration methods failed"
- Check Replicate status: https://replicate.com/status
- Verify your API token is valid
- Try a different image (JPEG/PNG recommended)

### Rate Limiting
- The system automatically retries with backoff
- If persistent, wait a few minutes between requests
- Check if other apps are using the same token

## 9. Differences from HuggingFace Provider

| Feature | Replicate Free | HuggingFace Free |
|---------|---------------|------------------|
| Face Restoration | GFPGAN/CodeFormer | CodeFormer/GFPGAN |
| Upscaling | Real-ESRGAN | Real-ESRGAN |
| Colorization | ❌ Not available | ✅ DeOldify |
| Speed | Moderate | Variable (depends on queue) |
| Reliability | High (paid service) | Medium (community spaces) |
| API Stability | High | Medium |

## 10. Next Steps

- Test with your own images
- Monitor your usage on Replicate dashboard
- Consider upgrading to paid tier if needed
- Check logs for detailed processing information

## Support

For detailed documentation, see:
- [REPLICATE_FREE_TIER.md](REPLICATE_FREE_TIER.md) - Full technical documentation
- [Replicate Documentation](https://replicate.com/docs) - Official Replicate docs
- [Model URLs](#model-urls) - Direct links to models

## Model URLs

- GFPGAN: https://replicate.com/tencentarc/gfpgan
- CodeFormer: https://replicate.com/sczhou/codeformer
- Real-ESRGAN: https://replicate.com/nightmareai/real-esrgan
