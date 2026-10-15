import { useState } from "react";
import { KeyRound } from "lucide-react";

import { getAdminKey, setAdminKey } from "../data/team";


// onSaved may return a promise (the request that checks the key);
// the button stays disabled until it settles.
function AdminKeyForm({ rejected, onSaved }) {
  const [value, setValue] = useState("");
  const [checking, setChecking] = useState(false);

  async function submit(event) {
    event.preventDefault();

    if (!value.trim() || checking) {
      return;
    }

    setAdminKey(value.trim());
    setChecking(true);

    try {
      await onSaved();
    } finally {
      setChecking(false);

      // A rejected key has been forgotten: start the field empty again.
      if (!getAdminKey()) {
        setValue("");
      }
    }
  }

  return (
    <form className="settings-panel admin-key-panel" onSubmit={submit}>
      <div className="settings-panel-heading">
        <h2><KeyRound size={15} /> Team admin key</h2>
        <p role={rejected ? "alert" : undefined}>
          {rejected
            ? "That key was not accepted. Paste the key printed by the API server."
            : "Team data is only shown to the main user. Paste the admin key printed when the API server starts."}
        </p>
      </div>

      <label className="settings-field">
        <span>Admin key</span>
        <input
          type="password"
          autoComplete="off"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="From the server terminal, or data/team/admin_key"
        />
      </label>

      <button
        type="submit"
        className="settings-save-button team-key-button"
        disabled={checking || !value.trim()}
        aria-busy={checking}
      >
        {checking ? "Checking key..." : "Unlock team analytics"}
      </button>
    </form>
  );
}


export default AdminKeyForm;
