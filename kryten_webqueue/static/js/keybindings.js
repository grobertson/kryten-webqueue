/* kryten-webqueue — Keyboard shortcuts
 *
 * A tiny global registry so pages can bind keys without each re-implementing
 * "ignore keystrokes while the user is typing / a modal is open" logic.
 *
 * Usage:
 *   Keybindings.register('ArrowRight', () => { ... });   // return false to fall through
 *   Keybindings.unregister('ArrowRight');
 *
 * Key strings are modifier-prefixed and use the raw KeyboardEvent.key value,
 * e.g. 'ArrowLeft', 'ctrl+k', 'shift+/'.
 */
(function () {
    const bindings = new Map();

    function isEditableTarget(el) {
        if (!el) return false;
        const tag = el.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
        return !!el.isContentEditable;
    }

    function keyFromEvent(e) {
        const parts = [];
        if (e.ctrlKey) parts.push('ctrl');
        if (e.altKey) parts.push('alt');
        if (e.shiftKey) parts.push('shift');
        if (e.metaKey) parts.push('meta');
        parts.push(e.key);
        return parts.join('+');
    }

    function onKeyDown(e) {
        // Never steal keys while typing in a field.
        if (isEditableTarget(document.activeElement)) return;
        // Leave an open modal to its own keyboard handling.
        if (document.querySelector('.modal-overlay')) return;

        const handler = bindings.get(keyFromEvent(e));
        if (!handler) return;

        // Handlers return false to decline (e.g. no target to act on), in which
        // case we leave the browser's default behaviour intact.
        if (handler(e) !== false) {
            e.preventDefault();
        }
    }

    document.addEventListener('keydown', onKeyDown);

    window.Keybindings = {
        register(key, handler) {
            bindings.set(key, handler);
        },
        unregister(key) {
            bindings.delete(key);
        },
    };
})();
