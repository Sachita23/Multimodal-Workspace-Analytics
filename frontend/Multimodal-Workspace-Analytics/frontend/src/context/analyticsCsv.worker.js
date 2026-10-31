// Parses /api/analytics.csv off the main thread (see csvParser.js).

import { handleParseRequest } from "./csvTransport.js";


self.onmessage = (event) => {
  const { message, transfer } = handleParseRequest(event.data);
  self.postMessage(message, transfer);
};
