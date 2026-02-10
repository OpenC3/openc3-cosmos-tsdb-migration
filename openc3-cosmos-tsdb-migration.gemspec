# encoding: ascii-8bit

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

# Create the overall gemspec
Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-tsdb-migration'
  s.summary = 'TSDB Migration'
  s.description = <<-EOF
    Migrate existing COSMOS bin file data into the OpenC3 Time Series Database (TSDB).
  EOF
  s.license = 'OpenC3'
  s.authors = ['Jason Thomas']
  s.email = ['jason@openc3.com']
  s.homepage = 'https://github.com/OpenC3/cosmos-enterprise-plugins/tree/main/openc3-cosmos-tsdb-migration#readme'
  s.platform = Gem::Platform::RUBY
  s.required_ruby_version = '>= 3.0'
  s.metadata = {
    'openc3_store_keywords' => 'tsdb,migration,questdb',
    'source_code_uri' => 'https://github.com/OpenC3/cosmos-enterprise-plugins/tree/main/openc3-cosmos-tsdb-migration',
    'openc3_store_access_type' => 'public',
    'openc3_cosmos_minimum_version' => '7.0.0'
  }

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    time = Time.now.strftime("%Y%m%d%H%M%S")
    s.version = '0.0.0' + ".#{time}"
  end
  s.files = Dir.glob("{lib,public,microservices}/**/*") + %w(Rakefile README.md LICENSE.md plugin.txt pyproject.toml poetry.lock)
end
