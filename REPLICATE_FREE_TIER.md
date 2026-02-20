# Replicate Free Tier Integration

This document describes the updated Replicate integration that uses **FREE tier models** for photo restoration.

## Overview

The updated `ReplicateProvider` now uses free Replicate models with an intelligent fallback strategy to ensure reliable photo restoration without requiring paid credits.

## Free Models Used

### 1. GFPGAN (Primary Method)
- **Model**: `tencentarc/gfpgan`
- **URL**: https://replicate.com/tencentarc/gfpgan
- **Version**: `0fbacf7afc6c144e5be9767cff80f25aff23e52b0708f17e20f9879b2f21516c`
- **Best for**: Old photo restoration and face enhancement
- **Parameters**:
  - `img`: Input image URL
  - `version`: "v1.4" (best quality)
  - `scale`: 2 (2x upscaling)

### 2. CodeFormer (Fallback Method)
- **Model**: `sczhou/codeformer`
- **URL**: https://replicate.com/sczhou/codeformer
- **Version**: `7de2ea26c616d5bf2245ad0d5e24f0ff9a6204578a5c876db53142edd9d2cd56`
- **Best for**: Face restoration with quality/fidelity control
- **Parameters**:
  - `image`: Input image URL
  - `upscale`: 2 (2x upscaling)
  - `codeformer_fidelity`: 0.7 (balance between quality and identity preservation)

### 3. Real-ESRGAN (Last Resort)
- **Model**: `nightmareai/real-esrgan`
- **URL**: https://replicate.com/nightmareai/real-esrgan
- **Version**: `f121d640bd286e1fdc67f9799164c1d5be36ff74576ee11c803ae5b665dd46aa`
- **Best for**: Image upscaling when face enhancement fails
- **Parameters**:
  - `image`: Input image URL
  - `scale`: 2 (2x upscaling)
  - `face_enhance`: True

## Fallback Strategy

The implementation uses a smart fallback strategy to ensure photo restoration always succeeds:

```
1. Try GFPGAN (best for old photos with faces)
   ↓ If fails
2. Try CodeFormer (alternative face enhancement)
   ↓ If fails
3. Use Real-ESRGAN (upscaling only)
   ↓ If all fail
4. Return error message
```

### Why This Strategy?

1. **GFPGAN First**: Best quality for old photos and face restoration
2. **CodeFormer Fallback**: Alternative approach with different strengths
3. **Real-ESRGAN Last**: Ensures we at least upscale the image even if face detection fails
4. **Comprehensive Error Handling**: Logs all attempts for debugging

## Key Features

### 1. Comprehensive Error Handling
- Each model attempt is wrapped in try-catch
- Detailed logging of failures with truncated error messages
- Graceful degradation to simpler methods

### 2. Rate Limit Handling
- Automatic retry with exponential backoff (5s, 10s, 15s, 20s)
- Clear error messages for quota/billing issues
- Model-specific rate limit tracking

### 3. Progress Tracking
- Real-time progress updates via callback
- Clear status messages for each processing stage
- Percentage-based progress (10%, 20%, 95%, 100%)

### 4. Model Version Pinning
- All models use pinned version IDs
- Ensures consistent behavior over time
- Easy to update when newer versions are available

### 5. Proper Face Detection
- GFPGAN and CodeFormer both handle face detection
- Real-ESRGAN has face_enhance option
- Automatic fallback if face detection fails

## Changes from Previous Implementation

### Before (Expensive Models)
```python
# Used expensive/paid models:
- GFPGAN (same, but no fallback)
- Real-ESRGAN (expensive version)
- DDColor (paid colorization)
```

### After (Free Models)
```python
# Uses free models with fallback:
- GFPGAN (tencentarc/gfpgan) - FREE
- CodeFormer (sczhou/codeformer) - FREE
- Real-ESRGAN (nightmareai/real-esrgan) - FREE
- Removed colorization (not free)
```

## Configuration

### Environment Variables

```bash
# Required: Set your Replicate API token
REPLICATE_API_TOKEN=your_actual_token_here

# Set provider to use Replicate
ai_provider=replicate
```

### Getting a Replicate API Token

1. Sign up at https://replicate.com
2. Go to https://replicate.com/account/api-tokens
3. Create a new API token
4. Add it to your `.env` file

## Testing

### Quick Test

```bash
# Install dependencies
cd backend
pip install -e .

# Run test with a sample image
python test_replicate_free.py uploads/test_photo.jpg
```

### Expected Output

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

Check the output file to verify the restoration quality.
============================================================
```

## Cost Comparison

### Old Implementation (Estimated)
- GFPGAN: ~$0.005/image
- Real-ESRGAN: ~$0.010/image
- DDColor: ~$0.015/image
- **Total**: ~$0.030/image

### New Implementation
- GFPGAN: **FREE**
- CodeFormer: **FREE**
- Real-ESRGAN: **FREE**
- **Total**: **$0.00/image** (within free tier limits)

### Free Tier Limits
Replicate's free tier typically includes:
- Limited API calls per month
- Rate limiting (handled with backoff)
- Queue priority (slower than paid)

Check current limits at: https://replicate.com/pricing

## Code Structure

### New Methods

```python
async def _try_gfpgan(...)
    """Try GFPGAN for face restoration. Best for old photos."""

async def _try_codeformer(...)
    """Try CodeFormer for face restoration. Alternative to GFPGAN."""

async def _try_real_esrgan(...)
    """Try Real-ESRGAN for upscaling. Fallback when face enhancement fails."""
```

### Updated Methods

```python
async def _run_model(..., model_name: str)
    """Now includes model_name for better logging."""

async def process_photo(...)
    """Implements fallback strategy with proper error handling."""
```

## Logging

The implementation includes comprehensive logging:

```python
import logging
logger = logging.getLogger("artimagehub.replicate")

# Log levels used:
logger.info()     # Success messages, model attempts
logger.warning()  # Fallback attempts, skipped steps
logger.error()    # Complete failures
```

Enable logging to see detailed processing info:

```python
import logging
logging.basicConfig(level=logging.INFO)
```

## Limitations

### 1. No Colorization
- DDColor and DeOldify are not in the free tier
- Colorization requests will be logged and skipped
- Consider using HuggingFace provider for colorization

### 2. Rate Limits
- Free tier has API call limits
- May experience queuing during high usage
- Automatic retry handles temporary rate limits

### 3. Processing Time
- Free tier may be slower than paid tier
- Queue priority is lower
- Timeout set to 120 seconds per model

## Troubleshooting

### Error: "Replicate credits exhausted (402)"
**Solution**: You've exceeded free tier limits. Either:
- Wait for limits to reset (usually monthly)
- Add billing at https://replicate.com/account/billing

### Error: "Rate limit exceeded after retries"
**Solution**: Too many requests in short time.
- Wait a few minutes
- Check if you have other applications using the same token

### Error: "All restoration methods failed"
**Solution**: All three models failed.
- Check Replicate status page
- Verify your API token is valid
- Check image format (JPEG/PNG recommended)

### Error: "REPLICATE_API_TOKEN environment variable not set"
**Solution**: Add token to `.env` file:
```bash
REPLICATE_API_TOKEN=your_token_here
```

## Future Improvements

Potential enhancements:

1. **Adaptive Scaling**: Choose scale factor based on input resolution
2. **Batch Processing**: Process multiple images in parallel
3. **Caching**: Cache results to avoid re-processing same images
4. **Model Selection**: Allow users to choose specific models
5. **Quality Metrics**: Return quality scores for each output

## Support

For issues or questions:

1. Check logs for detailed error messages
2. Verify API token and configuration
3. Test with the provided test script
4. Check Replicate model pages for status
5. Review Replicate documentation: https://replicate.com/docs

## Related Files

- `/Users/zj-db0812/vibecoding/photofix/backend/app/services/ai_service.py` - Main implementation
- `/Users/zj-db0812/vibecoding/photofix/backend/test_replicate_free.py` - Test script
- `/Users/zj-db0812/vibecoding/photofix/backend/app/config.py` - Configuration
- `/Users/zj-db0812/vibecoding/photofix/backend/.env` - Environment variables
