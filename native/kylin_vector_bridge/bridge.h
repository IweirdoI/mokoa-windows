#pragma once
#ifdef _WIN32
#define MOKOA_EXPORT __declspec(dllexport)
#else
#define MOKOA_EXPORT __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif
/* ABI 1: UTF-8 JSON, opaque connection handles, caller must free each response.
 * No C++ object or exception crosses this boundary. Calls on one handle are
 * serialized. Destroy requires that no calls remain in flight.
 */
MOKOA_EXPORT int mokoa_vector_abi_version(void);
MOKOA_EXPORT void* mokoa_vector_create(void);
MOKOA_EXPORT char* mokoa_vector_call(void* handle, const char* request);
MOKOA_EXPORT void mokoa_vector_free(void* response);
MOKOA_EXPORT void mokoa_vector_destroy(void* handle);
#ifdef __cplusplus
}
#endif
