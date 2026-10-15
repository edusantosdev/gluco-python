cask "gluco" do
  version "1.0.8"
  sha256 "a569b99bf49c26672e64faab862c4305b368b7aedd478a26119d68b4049b2871"

  url "https://github.com/edusantosdev/gluco-python/releases/download/v#{version}/Gluco-v#{version}-macos.zip"
  name "Gluco"
  desc "macOS menubar glucose display backed by LibreView"
  homepage "https://github.com/edusantosdev/gluco-python"

  app "Gluco.app"

  zap trash: [
    "~/.glucobar",
    "~/Library/LaunchAgents/com.glucobar.frontend.plist",
  ]
end
