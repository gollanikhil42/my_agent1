import React from "react";
import ReactDOM from "react-dom/client";
import { Authenticator } from "@aws-amplify/ui-react";
import { signUp } from "aws-amplify/auth";
import "@aws-amplify/ui-react/styles.css";
import App from "./App";
import "./styles.css";
import "./awsConfig";

const authServices = {
  async handleSignUp(input) {
    const username = input?.username || "";
    const password = input?.password || "";
    const options = input?.options || {};
    const attrs = { ...(options?.userAttributes || {}) };
    // Cognito requires name.formatted when 'name' is a required attribute in the user pool.
    // Always sync it from 'name' so the schema requirement is satisfied.
    if (attrs["name"]) {
      attrs["name.formatted"] = attrs["name"];
    }

    return signUp({
      username,
      password,
      options: {
        ...options,
        userAttributes: attrs,
      },
    });
  },
};

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Authenticator services={authServices}>
      {({ signOut, user }) => <App signOut={signOut} user={user} />}
    </Authenticator>
  </React.StrictMode>
);
