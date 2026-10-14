import { useState } from "react";
import { KeyRound } from "lucide-react";

import { setAdminKey } from "../data/team";


function AdminKeyForm({ rejected, onSaved }) {
  const [value, setValue] = useState("");

  function submit(event) {
    event.preventDefault();

    if (!value.trim()) {
      return;
    }

    setAdminKey(value.trim());
    onSaved();
  }

  return (
    <form className="settings-panel admin-key-panel" onSubmit={submit}>
      <div className="settings-panel-heading">
        <h2><KeyRound size={15} /> Team admin key</h2>
        <p>
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

      <button type="submit" className="settings-save-button team-key-button">
        Unlock team analytics
      </button>
    </form>
  );
}


export default AdminKeyForm;
