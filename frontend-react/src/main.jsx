import React from "react";
import ReactDOM from "react-dom/client";
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import App from "./App";
import "./styles.css";
import "./awsConfig";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Authenticator>
      {({ signOut, user }) => <App signOut={signOut} user={user} />}
    </Authenticator>
  </React.StrictMode>
);
