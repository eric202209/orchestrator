/** Format only public API message fields; never stringify response objects. */
export function apiErrorMessage(error: unknown, fallback = 'Request failed. Please try again.'): string {
  const record = (value: unknown): Record<string, unknown> | undefined =>
    value !== null && typeof value === 'object' ? value as Record<string, unknown> : undefined;
  const readable = (value: unknown): string | undefined =>
    typeof value === 'string' && value.trim() ? value : undefined;
  const response = record(record(error)?.response);
  const detail = record(response?.data)?.detail;
  return readable(detail)
    || readable(record(detail)?.message)
    || readable(record(detail)?.code)
    // Transport errors have no server response. Do not expose arbitrary error text.
    || (!response && record(error)?.code === 'ERR_NETWORK' ? 'Network error. Check your connection and try again.' : undefined)
    || (!response && record(error)?.code === 'ECONNABORTED' ? 'Request timed out. Please try again.' : undefined)
    || fallback;
}
