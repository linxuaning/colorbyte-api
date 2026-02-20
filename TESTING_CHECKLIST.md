# Testing Checklist: Replicate Free Tier Integration

## Pre-Testing Setup

- [ ] Get Replicate API token from https://replicate.com/account/api-tokens
- [ ] Add token to `.env` file: `REPLICATE_API_TOKEN=r8_...`
- [ ] Set provider in `.env`: `ai_provider=replicate`
- [ ] Activate virtual environment: `source .venv/bin/activate`
- [ ] Verify dependencies installed: `pip list | grep -E "httpx|replicate"`

## Unit Tests

### Test 1: Basic Import Test
```bash
cd backend
source .venv/bin/activate
python -c "from app.services.ai_service import ReplicateProvider; print('✓ Import successful')"
```
- [ ] Imports without errors
- [ ] No syntax warnings

### Test 2: Configuration Test
```bash
python -c "from app.config import get_settings; s = get_settings(); print(f'Provider: {s.ai_provider}'); print(f'Token set: {bool(s.replicate_api_token)}')"
```
- [ ] Provider is 'replicate'
- [ ] Token is set (not empty)

### Test 3: Service Initialization
```bash
python -c "from app.services.ai_service import get_ai_service; s = get_ai_service(); print(f'Service type: {type(s._provider).__name__}')"
```
- [ ] Returns 'ReplicateProvider'
- [ ] No initialization errors

## Integration Tests

### Test 4: Basic Photo Restoration
```bash
python test_replicate_free.py uploads/49aec47cbde44ddf8acb76d0fdf8cd4c.jpg
```
**Expected:**
- [ ] Upload completes (10%)
- [ ] GFPGAN starts (20%)
- [ ] Processing completes
- [ ] Download succeeds (95%)
- [ ] File saved to results/
- [ ] Output file exists and is valid image

**Check Output:**
- [ ] File size > 0 bytes
- [ ] Can open image in viewer
- [ ] Image quality is improved
- [ ] Faces are enhanced (if present)

### Test 5: GFPGAN Success Path
**With a clear face photo:**
```bash
python test_replicate_free.py uploads/test_face_photo.jpg
```
- [ ] GFPGAN succeeds
- [ ] No fallback to CodeFormer
- [ ] Additional upscaling applied
- [ ] Total time < 60 seconds
- [ ] Output quality is good

### Test 6: Fallback Strategy
**Test with potentially challenging image:**
- [ ] If GFPGAN fails, CodeFormer is attempted
- [ ] If CodeFormer fails, Real-ESRGAN is attempted
- [ ] Appropriate warnings logged
- [ ] Final output still produced

### Test 7: Error Handling
**Test with invalid token:**
```bash
# Temporarily set invalid token in .env
REPLICATE_API_TOKEN=invalid_token_test
python test_replicate_free.py uploads/test.jpg
```
- [ ] Clear error message displayed
- [ ] No crash or exception leak
- [ ] Returns proper error status

## Performance Tests

### Test 8: Processing Time
**Measure end-to-end processing:**
```bash
time python test_replicate_free.py uploads/test.jpg
```
- [ ] Complete in < 120 seconds
- [ ] Progress updates appear
- [ ] No hanging or timeouts

### Test 9: Multiple Images
**Process 3 different images sequentially:**
```bash
for img in uploads/*.jpg; do
    python test_replicate_free.py "$img"
done
```
- [ ] All process successfully
- [ ] No rate limit errors (or handled gracefully)
- [ ] Consistent quality

### Test 10: Rate Limiting
**If rate limited:**
- [ ] Automatic retry with backoff (5s, 10s, 15s, 20s)
- [ ] Clear warning messages
- [ ] Eventually succeeds or fails gracefully

## API Endpoint Tests

### Test 11: Through FastAPI
**Start the server and test through API:**
```bash
# Terminal 1: Start server
uvicorn app.main:app --reload

# Terminal 2: Upload and process
curl -X POST http://localhost:8000/api/upload \
  -F "file=@uploads/test.jpg" \
  -F "colorize=false"
```
- [ ] Upload succeeds
- [ ] Task created
- [ ] Processing completes
- [ ] Result downloadable

### Test 12: Progress Tracking
**Check progress updates:**
- [ ] Progress starts at 10% (upload)
- [ ] Updates to 20% (processing)
- [ ] Updates to 60% (upscaling)
- [ ] Reaches 95% (download)
- [ ] Completes at 100%

## Edge Cases

### Test 13: Large Image
**Test with high-resolution image (>5MB):**
- [ ] Upload succeeds
- [ ] Processing completes
- [ ] Output maintains quality
- [ ] No timeout errors

### Test 14: Small Image
**Test with tiny image (<100KB):**
- [ ] Processing succeeds
- [ ] Upscaling applied correctly
- [ ] Output is reasonable size

### Test 15: Different Formats
**Test various image formats:**
- [ ] JPEG: ✓
- [ ] PNG: ✓
- [ ] WebP: ✓
- [ ] Others: Document behavior

### Test 16: No Face Image
**Test with landscape/object photo (no faces):**
- [ ] GFPGAN may fail (expected)
- [ ] Falls back to CodeFormer or Real-ESRGAN
- [ ] Still produces upscaled output
- [ ] No crashes

### Test 17: Corrupted Image
**Test with invalid/corrupted image:**
- [ ] Proper error handling
- [ ] Clear error message
- [ ] No server crash

### Test 18: Colorization Request (Deprecated)
**Test colorize=True:**
```python
result = await provider.process_photo(..., colorize=True)
```
- [ ] Warning logged
- [ ] Processing continues (without color)
- [ ] Progress callback notified
- [ ] Completes successfully

## Model-Specific Tests

### Test 19: GFPGAN Model
- [ ] Uses correct version: `0fbacf...`
- [ ] Passes correct parameters
- [ ] Returns valid output URL
- [ ] Face enhancement visible

### Test 20: CodeFormer Model
- [ ] Uses correct version: `7de2ea...`
- [ ] Fidelity parameter: 0.7
- [ ] Upscale parameter: 2
- [ ] Quality good

### Test 21: Real-ESRGAN Model
- [ ] Uses correct version: `f121d6...`
- [ ] Scale parameter: 2
- [ ] Face enhance parameter set
- [ ] Upscaling works

## Logging Tests

### Test 22: Info Logging
```bash
# Enable info logging
python -c "
import logging
logging.basicConfig(level=logging.INFO)
# Run test
"
```
- [ ] Model attempts logged
- [ ] Success messages appear
- [ ] Timing information available

### Test 23: Error Logging
**Force errors and check logs:**
- [ ] GFPGAN failures logged
- [ ] CodeFormer fallback logged
- [ ] Final error includes all attempts
- [ ] Error messages truncated properly

## Resource Tests

### Test 24: Memory Usage
**Monitor memory during processing:**
```bash
# macOS
top -pid $(pgrep -f "python test_replicate")
```
- [ ] No memory leaks
- [ ] Reasonable memory usage
- [ ] Clean shutdown

### Test 25: File Cleanup
**Check temp files:**
- [ ] No orphaned temp files
- [ ] Proper cleanup on error
- [ ] Output files in correct location

## Security Tests

### Test 26: API Token Security
- [ ] Token not logged in plain text
- [ ] Token not in error messages
- [ ] Token not in output files

### Test 27: File Path Security
- [ ] No path traversal possible
- [ ] Files saved to intended directories
- [ ] Input validation working

## Documentation Tests

### Test 28: README Accuracy
- [ ] Code examples work as written
- [ ] Model URLs are correct
- [ ] Version numbers match
- [ ] Screenshots accurate (if any)

### Test 29: Error Messages Match Docs
- [ ] Documented errors appear as described
- [ ] Troubleshooting steps work
- [ ] Links are valid

## Deployment Tests

### Test 30: Environment Variables
**Production environment:**
- [ ] REPLICATE_API_TOKEN set correctly
- [ ] ai_provider configured
- [ ] No debug mode in production

### Test 31: Container Test (if using Docker)
```bash
docker build -t photofix-backend .
docker run -e REPLICATE_API_TOKEN=xxx photofix-backend
```
- [ ] Container builds
- [ ] Dependencies installed
- [ ] Service starts
- [ ] API accessible

## Sign-off

### Required Before Production
- [ ] All critical tests passing (Tests 1-12)
- [ ] Edge cases handled (Tests 13-18)
- [ ] Models working correctly (Tests 19-21)
- [ ] Security verified (Tests 26-27)
- [ ] Documentation accurate (Tests 28-29)

### Performance Benchmarks
- Average processing time: _____ seconds
- Success rate: _____%
- Fallback frequency: _____%
- Most common successful model: _____

### Known Issues
List any known issues or limitations:
1.
2.
3.

### Recommendations
- [ ] Monitor Replicate usage dashboard
- [ ] Set up alerts for rate limiting
- [ ] Track model success rates
- [ ] Consider paid tier if free limits exceeded

### Tested By
- Name: _________________
- Date: _________________
- Version: ______________

### Approved By
- Name: _________________
- Date: _________________
- Signature: ____________
