# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
# LICENSE.md for more details
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

PLUGIN_NAME = Dir['*.gemspec'][0].split('.')[0..-2].join('.')

task :require_version do
  unless ENV['VERSION']
    puts "VERSION is required: rake build VERSION=X.Y.Z"
    exit 1
  end
end

# Note: The sh command is built into Rake:
# https://rubydoc.info/gems/rake/FileUtils#sh-instance_method
task :build => [:require_version] do
  _, platform, *_ = RUBY_PLATFORM.split("-")
  if platform == 'mswin32' or platform == 'mingw32'
    puts "Warning: Building gem on Windows will lose file permissions"
  end

  # Run pnpm build if we have a package.json
  if File.exist?('package.json')
    sh('pnpm run build') do |ok, status|
      raise "pnpm run build failed" if !ok
    end
  end

  split_version = ENV['VERSION'].to_s.split('.')
  major = split_version[0]
  minor = split_version[1]
  if ENV['VERSION'] =~ /[a-zA-Z]+/
    # Prerelease version
    remainder = split_version[2..-1].join(".")
    remainder.gsub!('-', '.pre.') # Rubygems replaces dashes with .pre.
    remainder_split = remainder.split('.')
    patch = remainder_split[0]
    other = remainder_split[1..-1].join('.')
    gem_version = "#{major}.#{minor}.#{patch}.#{other}"
  else
    gem_version = ENV['VERSION']
  end
  gem_name = PLUGIN_NAME + '-' + gem_version + '.gem'

  sh('gem', 'build', PLUGIN_NAME)
  sh("openc3cli validate #{gem_name}") do |ok, status|
    if !ok && status.exitstatus == 127 # command not found
      puts "Install the openc3 gem to validate! (gem install openc3)"
    end
  end
end
