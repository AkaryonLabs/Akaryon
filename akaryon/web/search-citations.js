(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AkaryonSearchCitations = api;
})(typeof globalThis === 'undefined' ? this : globalThis, function () {
  'use strict';

  function isSafeSourceUrl(value) {
    if (typeof value !== 'string') return false;
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/\.$/, '');
      return (url.protocol === 'http:' || url.protocol === 'https:') &&
        !url.username && !url.password && host !== 'localhost' &&
        !host.endsWith('.localhost') && !host.endsWith('.local') &&
        !host.endsWith('.internal') && !isNonPublicIp(host);
    } catch {
      return false;
    }
  }

  function isNonPublicIp(host) {
    const address = host.replace(/^\[|\]$/g, '');
    const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(address);
    if (v4) {
      const octets = v4.slice(1).map(Number);
      if (octets.some(value => value > 255)) return true;
      const [a, b, c] = octets;
      return a === 0 || a === 10 || a === 127 || a >= 224 ||
        (a === 100 && b >= 64 && b <= 127) ||
        (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
        (a === 192 && (b === 168 || (b === 0 && c === 2))) ||
        (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
        (a === 203 && b === 0 && c === 113);
    }
    if (address.includes(':')) {
      const ipv4Mapped = /::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(address);
      return !!ipv4Mapped || address === '::' || address === '::1' ||
        /^f[cd]/i.test(address) || /^fe[89ab]/i.test(address);
    }
    return false;
  }

  function inlineCitationSegments(answer, citations) {
    if (typeof answer !== 'string' || !Array.isArray(citations)) return [];
    const spans = citations.filter(item => item && isSafeSourceUrl(item.url) &&
      Number.isInteger(item.start_index) && Number.isInteger(item.end_index) &&
      item.start_index >= 0 && item.start_index < item.end_index &&
      item.end_index <= answer.length)
      .slice()
      .sort((left, right) => left.start_index - right.start_index);
    const segments = [];
    let offset = 0;
    for (const citation of spans) {
      if (citation.start_index < offset) continue;
      if (citation.start_index > offset) {
        segments.push({ type: 'text', text: answer.slice(offset, citation.start_index) });
      }
      segments.push({
        type: 'citation',
        text: answer.slice(citation.start_index, citation.end_index),
        url: citation.url,
        title: typeof citation.title === 'string' ? citation.title : citation.url,
      });
      offset = citation.end_index;
    }
    if (offset < answer.length) segments.push({ type: 'text', text: answer.slice(offset) });
    return segments;
  }

  return { inlineCitationSegments, isSafeSourceUrl };
});
