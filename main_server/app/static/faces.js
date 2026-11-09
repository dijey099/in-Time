// Small wrapper around face-api.js used by the admin pages to compute the
// 128-value face descriptor of a user's reference photo.
const Faces = (() => {
  let loaded = null;

  function load(modelUrl) {
    if (!loaded) {
      loaded = Promise.all([
        faceapi.nets.ssdMobilenetv1.loadFromUri(modelUrl),
        faceapi.nets.faceLandmark68Net.loadFromUri(modelUrl),
        faceapi.nets.faceRecognitionNet.loadFromUri(modelUrl),
      ]);
    }
    return loaded;
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('cannot load image'));
      img.src = src;
    });
  }

  // -> {descriptor: number[128], box} or {error}
  async function describe(img) {
    const options = new faceapi.SsdMobilenetv1Options({minConfidence: 0.5});
    const all = await faceapi.detectAllFaces(img, options).withFaceLandmarks().withFaceDescriptors();
    if (all.length === 0) return {error: 'no face detected'};
    if (all.length > 1) return {error: `${all.length} faces detected, use a photo with one face`};
    return {descriptor: Array.from(all[0].descriptor), box: all[0].detection.box};
  }

  // Light detector used for the live camera hint
  let liveLoaded = null;
  function loadLive(modelUrl) {
    if (!liveLoaded) liveLoaded = faceapi.nets.tinyFaceDetector.loadFromUri(modelUrl);
    return liveLoaded;
  }

  async function countFaces(input) {
    const faces = await faceapi.detectAllFaces(input, new faceapi.TinyFaceDetectorOptions({inputSize: 416, scoreThreshold: 0.4}));
    return faces.length;
  }

  return {load, loadImage, describe, loadLive, countFaces};
})();
