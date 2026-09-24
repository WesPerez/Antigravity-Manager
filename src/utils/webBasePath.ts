import { isTauri } from './env';

/** Read the deployment prefix from an explicit, same-origin HTML base tag. */
export function getWebBasename(): string {
    if (isTauri() || typeof document === 'undefined') return '/';

    const href = document.querySelector('base[href]')?.getAttribute('href');
    if (!href) return '/';

    try {
        const base = new URL(href, `${window.location.origin}/`);
        if (base.origin !== window.location.origin) return '/';
        return base.pathname.replace(/\/+$/, '') || '/';
    } catch {
        return '/';
    }
}

/** Resolve app-owned API and public asset paths without changing the API routes. */
export function webUrl(path: string): string {
    const basename = getWebBasename();
    return `${basename === '/' ? '' : basename}/${path.replace(/^\/+/, '')}`;
}
