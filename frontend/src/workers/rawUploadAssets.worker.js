import { LibRaw } from '@colorhythm/libraw-wasm';
import wasmUrl from '@colorhythm/libraw-wasm/libraw.wasm?url';
import { generateUploadPreview, generateUploadThumbnail } from '../utils/uploadDerivatives';

const clock = () => performance.now();

// Nikon's "High Efficiency"/"High Efficiency*" NEF compression (Z50 II, Z5 II, and any
// body outside LibRaw's hardcoded allowlist of Z9/Z8/Zf/Z6III) wraps the sensor-data strip
// in a JPEG-XS codestream (ISO 21122) starting with this SOC+CAP marker. LibRaw-WASM here
// is built on the same LibRaw as the backend's rawpy/dcraw_emu tiers, which confirmed
// decoding this as ordinary lossless-compressed data either raises mid-unpack or -- unlike
// rawpy, which raises cleanly -- silently renders noise with no error at all. Bailing out
// before decoder.unpack() lets the file fall through to the backend's exiftool-based
// embedded-preview extraction, which never touches the HE bitstream.
const NIKON_HE_SOC_MARKER = [0xff, 0x10, 0xff, 0x50];

const containsNikonHeMarker = buffer => {
  const bytes = new Uint8Array(buffer);
  outer: for (let i = 0; i <= bytes.length - NIKON_HE_SOC_MARKER.length; i += 1) {
    for (let j = 0; j < NIKON_HE_SOC_MARKER.length; j += 1) {
      if (bytes[i + j] !== NIKON_HE_SOC_MARKER[j]) continue outer;
    }
    return true;
  }
  return false;
};

// Camera bodies where this LibRaw-WASM build doesn't expose the active-image crop
// rect (LibRaw's raw_inset_crop) through its public API -- confirmed: Sony DSC-RX1R
// III. getActiveWidth/Height, getIWidth/Height, and the margin getters all silently
// report the full raw sensor readout for these bodies instead of the real
// photographed area, so decoder.dcrawMakeMemImage() would demosaic (and ship) the
// sensor's opaque calibration border as a solid green band along the image's
// right/bottom edge. The backend's rawpy-based fallback reads raw.sizes.crop_* (the
// same data under a different name) correctly and crops to it -- bail out here and
// let it handle these bodies instead of shipping a cropped-wrong preview.
const ACTIVE_AREA_UNKNOWN_MODELS = new Set(['DSC-RX1RM3']);

self.onmessage = async ({ data: { file, maxMegapixels = 120, maxBytes = Infinity } }) => {
  const start = clock();
  const result = { filename: file.name, originalBytes: file.size, source: 'sensor_raw', embeddedAllowed: false };
  let decoder;
  try {
    if (file.size > maxBytes) throw new Error('Resource guard: RAW byte limit');
    const readStart = clock();
    const buffer = await file.arrayBuffer();
    result.readMs = clock() - readStart;
    if (/\.(nef|nrw)$/i.test(file.name) && containsNikonHeMarker(buffer)) {
      throw new Error('Unsupported file format: Nikon High Efficiency NEF compression');
    }
    const initStart = clock();
    await LibRaw.initialize(fetch(wasmUrl));
    result.wasmInitMs = clock() - initStart;
    result.librawVersion = LibRaw.version();
    decoder = new LibRaw();
    await decoder.waitUntilReady();
    const openStart = clock();
    decoder.open(buffer);
    result.rawDimensions = [decoder.getRawWidth(), decoder.getRawHeight()];
    result.activeDimensions = [decoder.getActiveWidth(), decoder.getActiveHeight()];
    if (result.rawDimensions[0] * result.rawDimensions[1] > maxMegapixels * 1_000_000) {
      throw new Error(`Resource guard: sensor exceeds ${maxMegapixels}MP`);
    }
    result.orientationFlip = decoder.getFlip();
    const camera = decoder.getIParams();
    const metadata = decoder.getImgOther();
    result.metadata = {
      make: camera.normalized_make || camera.make,
      model: camera.normalized_model || camera.model,
      iso: metadata.iso_speed,
      shutter: metadata.shutter,
      aperture: metadata.aperture,
      focalLength: metadata.focal_len,
      timestamp: typeof metadata.timestamp === 'bigint' ? metadata.timestamp.toString() : metadata.timestamp,
    };
    if (ACTIVE_AREA_UNKNOWN_MODELS.has(camera.normalized_model || camera.model)) {
      throw new Error('Unsupported file format: active image area unknown for this camera body');
    }
    result.openAndMetadataMs = clock() - openStart;
    result.halfSize = Math.max(...result.activeDimensions) >= 4096 ? 1 : 0;
    decoder.setHalfSize(result.halfSize);
    decoder.setUseCameraWb(1);
    decoder.setOutputColor(1);
    decoder.setOutputBps(8);
    decoder.setNoAutoBright(1);
    const unpackStart = clock();
    decoder.unpack();
    result.unpackMs = clock() - unpackStart;
    const developStart = clock();
    // LibRaw applies orientation; do not apply EXIF rotation a second time.
    decoder.dcrawProcess();
    const image = decoder.dcrawMakeMemImage();
    result.developMs = clock() - developStart;
    result.developed = { width: image.width, height: image.height, colors: image.colors, bits: image.bits };
    result.wasmHeapCapacityBytes = LibRaw.module.HEAPU8.buffer.byteLength;
    if (image.bits !== 8 || ![1, 3].includes(image.colors)) throw new Error('Unsupported rendered pixel layout');
    const encodeStart = clock();
    const canvas = new OffscreenCanvas(image.width, image.height);
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas context unavailable');
    const rgba = new Uint8ClampedArray(image.width * image.height * 4);
    for (let pixel = 0; pixel < image.width * image.height; pixel += 1) {
      const input = pixel * image.colors;
      const output = pixel * 4;
      rgba[output] = image.data[input];
      rgba[output + 1] = image.data[input + (image.colors === 3 ? 1 : 0)];
      rgba[output + 2] = image.data[input + (image.colors === 3 ? 2 : 0)];
      rgba[output + 3] = 255;
    }
    context.putImageData(new ImageData(rgba, image.width, image.height), 0, 0);
    const generatedPreview = await generateUploadPreview(canvas);
    const generatedThumbnail = await generateUploadThumbnail(canvas);
    result.encodeMs = clock() - encodeStart;
    result.preview = { bytes: generatedPreview.blob.size, width: generatedPreview.width, height: generatedPreview.height, quality: generatedPreview.quality };
    result.thumbnail = { bytes: generatedThumbnail.blob.size, width: generatedThumbnail.width, height: generatedThumbnail.height, quality: generatedThumbnail.quality };
    result.previewBlob = generatedPreview.blob;
    result.thumbnailBlob = generatedThumbnail.blob;
    result.status = 'generated';
  } catch (error) {
    result.status = 'failed';
    result.error = String(error);
  } finally {
    decoder?.dispose();
  }
  result.totalMs = clock() - start;
  self.postMessage(result);
};
