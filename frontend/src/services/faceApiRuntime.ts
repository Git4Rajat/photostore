export const loadFaceApiRuntimeBundle = async (): Promise<any> => {
    const faceapiModule = await import('face-api.js/build/es6/index.js');
    return (faceapiModule as any)?.default || faceapiModule;
};
