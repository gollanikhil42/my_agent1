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
    const attrs = options?.userAttributes || {};
    const formattedName = attrs["name.formatted"] || attrs.name || username;

    return signUp({
      username,
      password,
      options: {
        ...options,
        userAttributes: {
          ...attrs,
          "name.formatted": formattedName,
        },
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
