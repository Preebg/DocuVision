export interface DocumentPage {
  /** Stable identity used when a page moves between pending documents. */
  id: string;
  /** One-based page number in the original PDF. */
  pageNumber: number;
  thumbnailUrl?: string;
}

export interface OriginalDocument {
  id: string;
  name: string;
  pages: DocumentPage[];
}

export interface PendingDocument {
  id: string;
  name: string;
  pages: DocumentPage[];
}

export interface SplitSubmissionPayload {
  originalId: string;
  newDocuments: Array<{
    pageRange: string;
    order: number;
  }>;
}

/**
 * Compact an ordered page sequence without losing deliberate reordering.
 * [1, 2, 3, 5, 7, 8] becomes "1-3, 5, 7-8".
 */
export function calculatePageRange(
  pages: readonly Pick<DocumentPage, "pageNumber">[],
): string {
  if (pages.length === 0) {
    return "";
  }

  const ranges: string[] = [];
  let start = pages[0].pageNumber;
  let previous = start;

  for (let index = 1; index <= pages.length; index += 1) {
    const current = pages[index]?.pageNumber;
    if (current === previous + 1) {
      previous = current;
      continue;
    }

    ranges.push(start === previous ? `${start}` : `${start}-${previous}`);
    if (current !== undefined) {
      start = current;
      previous = current;
    }
  }

  return ranges.join(", ");
}

export function buildSplitPayload(
  originalId: string,
  pendingDocuments: readonly PendingDocument[],
): SplitSubmissionPayload {
  return {
    originalId,
    newDocuments: pendingDocuments
      .filter((document) => document.pages.length > 0)
      .map((document, index) => ({
        pageRange: calculatePageRange(document.pages),
        order: index + 1,
      })),
  };
}

export function moveItem<T>(
  items: readonly T[],
  fromIndex: number,
  toIndex: number,
): T[] {
  if (
    fromIndex < 0 ||
    fromIndex >= items.length ||
    toIndex < 0 ||
    toIndex >= items.length ||
    fromIndex === toIndex
  ) {
    return [...items];
  }

  const next = [...items];
  const [item] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, item);
  return next;
}
