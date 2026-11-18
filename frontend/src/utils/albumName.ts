// Smart albums are saved as "Location: Malmö", "Person: Asha", "Uploaded: Jan 1" and so on. The kind is
// bookkeeping for the server; on screen the album is just its name.
const KIND_PREFIX = /^(?:Uploaded|Location|Person|Event|Tag\/Object|Tag|Date|Month|Year):\s+(?=\S)/;

export const albumDisplayName = (name: string | null | undefined): string => (name ?? '').replace(KIND_PREFIX, '');
